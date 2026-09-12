package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

type fileList []string

func (values *fileList) String() string {
	return strings.Join(*values, ",")
}

func (values *fileList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

type rpcRequest struct {
	JSONRPC string        `json:"jsonrpc"`
	ID      int           `json:"id"`
	Method  string        `json:"method"`
	Params  []interface{} `json:"params"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	Result json.RawMessage `json:"result"`
	Error  *rpcError       `json:"error"`
}

type summary struct {
	RPCURL                 string   `json:"rpc_url"`
	TargetsPath            string   `json:"targets_path"`
	TransitionPath         string   `json:"transition_path"`
	PriorMatchPaths        []string `json:"prior_match_paths"`
	OutputPath             string   `json:"output_path"`
	OutputSHA256           string   `json:"output_sha256"`
	InitiallyUnresolved    uint64   `json:"initially_unresolved"`
	PriorMatched           uint64   `json:"prior_matched"`
	PreviouslyTraceMatched uint64   `json:"previously_trace_matched"`
	TransitionRows         uint64   `json:"transition_rows"`
	UniqueBlocks           uint64   `json:"unique_blocks"`
	BlocksTraced           uint64   `json:"blocks_traced"`
	TraceEntries           uint64   `json:"trace_entries"`
	UniqueCandidates       uint64   `json:"unique_candidates"`
	NewMatches             uint64   `json:"new_matches"`
	Unresolved             uint64   `json:"unresolved"`
	ElapsedMilliseconds    int64    `json:"elapsed_milliseconds"`
}

type resolver struct {
	targets map[common.Hash]struct{}
	seen    map[common.Address]struct{}
	writer  *csv.Writer
	output  *os.File
	matches uint64
}

var matchHeader = []string{
	"secure_key",
	"address",
	"source",
	"block_number",
	"transaction_hash",
	"detail",
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func indexes(header []string) map[string]int {
	result := make(map[string]int, len(header))
	for index, name := range header {
		result[name] = index
	}
	return result
}

func parseHash(value string) (common.Hash, error) {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		return common.Hash{}, fmt.Errorf("invalid 32-byte hash %q", value)
	}
	return common.BytesToHash(decoded), nil
}

func loadTargets(path string) (map[common.Hash]struct{}, uint64) {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open targets: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read target header: %v", err)
	}
	columns := indexes(header)
	keyColumn, haveKey := columns["secure_key"]
	addressColumn, haveAddress := columns["address"]
	if !haveKey || !haveAddress {
		fatalf("target CSV must contain secure_key and address")
	}
	targets := make(map[common.Hash]struct{})
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read targets: %v", err)
		}
		if row[addressColumn] != "" {
			continue
		}
		key, err := parseHash(row[keyColumn])
		if err != nil {
			fatalf("read targets: %v", err)
		}
		targets[key] = struct{}{}
	}
	return targets, uint64(len(targets))
}

func consumeMatches(path string, targets map[common.Hash]struct{}) uint64 {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open match CSV %s: %v", path, err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read match header %s: %v", path, err)
	}
	columns := indexes(header)
	keyColumn, haveKey := columns["secure_key"]
	addressColumn, haveAddress := columns["address"]
	if !haveKey || !haveAddress {
		fatalf("match CSV %s must contain secure_key and address", path)
	}
	var consumed uint64
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read match CSV %s: %v", path, err)
		}
		key, err := parseHash(row[keyColumn])
		if err != nil {
			fatalf("read match CSV %s: %v", path, err)
		}
		if !common.IsHexAddress(row[addressColumn]) {
			fatalf("invalid address %q in %s", row[addressColumn], path)
		}
		address := common.HexToAddress(row[addressColumn])
		if crypto.Keccak256Hash(address.Bytes()) != key {
			fatalf("cryptographic mismatch for %s in %s", key.Hex(), path)
		}
		if _, wanted := targets[key]; wanted {
			delete(targets, key)
			consumed++
		}
	}
	return consumed
}

func loadTransitionBlocks(path string, targets map[common.Hash]struct{}) ([]uint64, uint64) {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open transitions: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read transition header: %v", err)
	}
	columns := indexes(header)
	keyColumn, haveKey := columns["secure_key"]
	blockColumn, haveBlock := columns["transition_block"]
	if !haveKey || !haveBlock {
		fatalf("transition CSV must contain secure_key and transition_block")
	}
	blockSet := make(map[uint64]struct{})
	var rows uint64
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read transitions: %v", err)
		}
		key, err := parseHash(row[keyColumn])
		if err != nil {
			fatalf("read transitions: %v", err)
		}
		if _, wanted := targets[key]; !wanted {
			continue
		}
		block, err := strconv.ParseUint(row[blockColumn], 10, 64)
		if err != nil {
			fatalf("invalid transition block %q: %v", row[blockColumn], err)
		}
		rows++
		if block != 0 {
			blockSet[block] = struct{}{}
		}
	}
	blocks := make([]uint64, 0, len(blockSet))
	for block := range blockSet {
		blocks = append(blocks, block)
	}
	sort.Slice(blocks, func(i, j int) bool { return blocks[i] < blocks[j] })
	return blocks, rows
}

func openOutput(path string, targets map[common.Hash]struct{}) (*os.File, *csv.Writer, uint64) {
	var previous uint64
	if _, err := os.Stat(path); err == nil {
		previous = consumeMatches(path, targets)
	} else if !os.IsNotExist(err) {
		fatalf("stat output: %v", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("open output: %v", err)
	}
	stat, err := file.Stat()
	if err != nil {
		file.Close()
		fatalf("stat output: %v", err)
	}
	writer := csv.NewWriter(file)
	if stat.Size() == 0 {
		if err := writer.Write(matchHeader); err != nil {
			file.Close()
			fatalf("write output header: %v", err)
		}
		writer.Flush()
		if err := writer.Error(); err != nil {
			file.Close()
			fatalf("flush output header: %v", err)
		}
		if err := file.Sync(); err != nil {
			file.Close()
			fatalf("sync output header: %v", err)
		}
	}
	return file, writer, previous
}

func (resolver *resolver) add(address common.Address, block uint64, txHash, detail string) {
	if _, duplicate := resolver.seen[address]; duplicate {
		return
	}
	resolver.seen[address] = struct{}{}
	key := crypto.Keccak256Hash(address.Bytes())
	if _, wanted := resolver.targets[key]; !wanted {
		return
	}
	if err := resolver.writer.Write([]string{
		key.Hex(),
		address.Hex(),
		"canonical_transition_trace",
		strconv.FormatUint(block, 10),
		txHash,
		detail,
	}); err != nil {
		fatalf("write trace match: %v", err)
	}
	resolver.writer.Flush()
	if err := resolver.writer.Error(); err != nil {
		fatalf("flush trace match: %v", err)
	}
	if err := resolver.output.Sync(); err != nil {
		fatalf("sync trace match: %v", err)
	}
	delete(resolver.targets, key)
	resolver.matches++
}

func decodeHex(value string) []byte {
	if !strings.HasPrefix(value, "0x") || len(value)%2 != 0 {
		return nil
	}
	decoded, err := hex.DecodeString(value[2:])
	if err != nil {
		return nil
	}
	return decoded
}

func (resolver *resolver) scanData(value string, block uint64, txHash, path string) {
	data := decodeHex(value)
	if len(data) == common.AddressLength {
		resolver.add(common.BytesToAddress(data), block, txHash, path)
		return
	}
	for _, offset := range []int{0, 4} {
		for index := offset; index+32 <= len(data); index += 32 {
			resolver.add(common.BytesToAddress(data[index:index+20]), block, txHash, path+";word-left")
			resolver.add(common.BytesToAddress(data[index+12:index+32]), block, txHash, path+";word-right")
		}
	}
	for index := 0; index < len(data); {
		opcode := data[index]
		index++
		if opcode < 0x60 || opcode > 0x7f {
			continue
		}
		size := int(opcode - 0x5f)
		if index+size > len(data) {
			break
		}
		if size == common.AddressLength {
			resolver.add(common.BytesToAddress(data[index:index+size]), block, txHash, path+";push20")
		}
		index += size
	}
}

func isAddressField(name string) bool {
	switch strings.ToLower(name) {
	case "from", "to", "address", "refundaddress", "author", "beneficiary":
		return true
	default:
		return false
	}
}

func isDataField(name string) bool {
	switch strings.ToLower(name) {
	case "input", "output", "init", "code", "data":
		return true
	default:
		return false
	}
}

func (resolver *resolver) walk(value interface{}, block uint64, txHash, path, field string) {
	switch typed := value.(type) {
	case map[string]interface{}:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			resolver.walk(typed[key], block, txHash, path+"."+key, key)
		}
	case []interface{}:
		for index, item := range typed {
			resolver.walk(item, block, txHash, fmt.Sprintf("%s[%d]", path, index), field)
		}
	case string:
		if isAddressField(field) {
			data := decodeHex(typed)
			if len(data) == common.AddressLength {
				resolver.add(common.BytesToAddress(data), block, txHash, path)
			}
		} else if isDataField(field) {
			resolver.scanData(typed, block, txHash, path)
		}
	}
}

func callTraceBlock(client *http.Client, url string, block uint64) ([]interface{}, error) {
	payload, err := json.Marshal(rpcRequest{
		JSONRPC: "2.0",
		ID:      1,
		Method:  "trace_block",
		Params:  []interface{}{fmt.Sprintf("0x%x", block)},
	})
	if err != nil {
		return nil, fmt.Errorf("encode trace request: %w", err)
	}
	var lastError error
	for attempt := 1; attempt <= 5; attempt++ {
		request, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(payload))
		if err != nil {
			return nil, fmt.Errorf("create trace request: %w", err)
		}
		request.Header.Set("Content-Type", "application/json")
		response, err := client.Do(request)
		if err != nil {
			lastError = err
			time.Sleep(time.Duration(attempt) * time.Second)
			continue
		}
		var result rpcResponse
		decodeErr := json.NewDecoder(response.Body).Decode(&result)
		closeErr := response.Body.Close()
		if decodeErr != nil {
			lastError = decodeErr
		} else if closeErr != nil {
			lastError = closeErr
		} else if response.StatusCode != http.StatusOK {
			lastError = fmt.Errorf("HTTP status %s", response.Status)
		} else if result.Error != nil {
			lastError = fmt.Errorf("RPC %d: %s", result.Error.Code, result.Error.Message)
		} else {
			var traces []interface{}
			if err := json.Unmarshal(result.Result, &traces); err != nil {
				return nil, fmt.Errorf("decode trace result for block %d: %w", block, err)
			}
			return traces, nil
		}
		time.Sleep(time.Duration(attempt) * time.Second)
	}
	return nil, fmt.Errorf("trace block %d after retries: %w", block, lastError)
}

func transactionHash(trace interface{}) string {
	object, ok := trace.(map[string]interface{})
	if !ok {
		return ""
	}
	value, ok := object["transactionHash"].(string)
	if !ok || len(decodeHex(value)) != common.HashLength {
		return ""
	}
	return strings.ToLower(value)
}

func fileSHA256(path string) string {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open output for hashing: %v", err)
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		fatalf("hash output: %v", err)
	}
	return hex.EncodeToString(hasher.Sum(nil))
}

func writeJSONAtomic(path string, value interface{}) {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fatalf("encode summary: %v", err)
	}
	data = append(data, '\n')
	partial := path + ".partial"
	if err := os.WriteFile(partial, data, 0o600); err != nil {
		fatalf("write summary: %v", err)
	}
	if err := os.Rename(partial, path); err != nil {
		fatalf("publish summary: %v", err)
	}
}

func main() {
	var priorMatches fileList
	var (
		rpcURL         = flag.String("rpc", "", "canonical archive JSON-RPC URL")
		targetsPath    = flag.String("targets", "", "claim CSV containing unresolved keys")
		transitionPath = flag.String("transitions", "", "account transition CSV")
		outputPath     = flag.String("output", "", "verified trace match CSV")
		summaryPath    = flag.String("summary", "", "summary JSON")
		timeout        = flag.Duration("timeout", 5*time.Minute, "per-request timeout")
		workers        = flag.Int("workers", 8, "concurrent trace requests")
	)
	flag.Var(&priorMatches, "match", "prior verified match CSV; repeatable")
	flag.Parse()
	if *rpcURL == "" || *targetsPath == "" || *transitionPath == "" || *outputPath == "" || *summaryPath == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *workers < 1 || *workers > 64 {
		fatalf("workers must be between 1 and 64")
	}

	started := time.Now()
	targets, initiallyUnresolved := loadTargets(*targetsPath)
	var priorMatched uint64
	for _, path := range priorMatches {
		priorMatched += consumeMatches(path, targets)
	}
	outputFile, writer, previouslyTraceMatched := openOutput(*outputPath, targets)
	blocks, transitionRows := loadTransitionBlocks(*transitionPath, targets)
	resolver := &resolver{
		targets: targets,
		seen:    make(map[common.Address]struct{}),
		writer:  writer,
		output:  outputFile,
	}
	client := &http.Client{Timeout: *timeout}

	type traceResult struct {
		traces []interface{}
		err    error
	}
	var blocksTraced, traceEntries uint64
	for chunkStart := 0; chunkStart < len(blocks); chunkStart += *workers {
		chunkEnd := chunkStart + *workers
		if chunkEnd > len(blocks) {
			chunkEnd = len(blocks)
		}
		results := make([]traceResult, chunkEnd-chunkStart)
		var wait sync.WaitGroup
		for index := range results {
			wait.Add(1)
			go func(index int) {
				defer wait.Done()
				results[index].traces, results[index].err = callTraceBlock(
					client,
					*rpcURL,
					blocks[chunkStart+index],
				)
			}(index)
		}
		wait.Wait()
		for index, result := range results {
			block := blocks[chunkStart+index]
			if result.err != nil {
				fatalf("%v", result.err)
			}
			blocksTraced++
			traceEntries += uint64(len(result.traces))
			for traceIndex, trace := range result.traces {
				resolver.walk(trace, block, transactionHash(trace), fmt.Sprintf("trace[%d]", traceIndex), "")
			}
		}
		if blocksTraced%25 == 0 {
			fmt.Fprintf(os.Stderr, "progress blocks=%d/%d traces=%d matches=%d unresolved=%d\n",
				blocksTraced, len(blocks), traceEntries, resolver.matches, len(targets))
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		outputFile.Close()
		fatalf("flush output: %v", err)
	}
	if err := outputFile.Sync(); err != nil {
		outputFile.Close()
		fatalf("sync output: %v", err)
	}
	if err := outputFile.Close(); err != nil {
		fatalf("close output: %v", err)
	}

	result := summary{
		RPCURL:                 *rpcURL,
		TargetsPath:            *targetsPath,
		TransitionPath:         *transitionPath,
		PriorMatchPaths:        priorMatches,
		OutputPath:             *outputPath,
		OutputSHA256:           fileSHA256(*outputPath),
		InitiallyUnresolved:    initiallyUnresolved,
		PriorMatched:           priorMatched,
		PreviouslyTraceMatched: previouslyTraceMatched,
		TransitionRows:         transitionRows,
		UniqueBlocks:           uint64(len(blocks)),
		BlocksTraced:           blocksTraced,
		TraceEntries:           traceEntries,
		UniqueCandidates:       uint64(len(resolver.seen)),
		NewMatches:             resolver.matches,
		Unresolved:             uint64(len(targets)),
		ElapsedMilliseconds:    time.Since(started).Milliseconds(),
	}
	writeJSONAtomic(*summaryPath, result)
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary output: %v", err)
	}
}
