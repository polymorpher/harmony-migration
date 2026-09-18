package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

const (
	depositTopic    = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
	withdrawTopic   = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
	transferTopic   = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
	totalSupplyCall = "0x18160ddd"
)

var zeroAddress common.Address

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *rpcError) Error() string {
	return fmt.Sprintf("JSON-RPC error %d: %s", e.Code, e.Message)
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      uint64          `json:"id"`
	Result  json.RawMessage `json:"result"`
	Error   *rpcError       `json:"error"`
}

type rpcClient struct {
	url    string
	http   *http.Client
	nextID atomic.Uint64
}

func newRPCClient(url string) *rpcClient {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 256
	transport.MaxIdleConnsPerHost = 256
	transport.MaxConnsPerHost = 256
	return &rpcClient{
		url: url,
		http: &http.Client{
			Transport: transport,
			Timeout:   2 * time.Minute,
		},
	}
}

func (c *rpcClient) call(
	ctx context.Context,
	method string,
	params any,
	result any,
) error {
	id := c.nextID.Add(1)
	payload, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"method":  method,
		"params":  params,
	})
	if err != nil {
		return fmt.Errorf("encode %s request: %w", method, err)
	}

	var lastError error
	for attempt := 0; attempt < 5; attempt++ {
		request, err := http.NewRequestWithContext(
			ctx,
			http.MethodPost,
			c.url,
			bytes.NewReader(payload),
		)
		if err != nil {
			return fmt.Errorf("create %s request: %w", method, err)
		}
		request.Header.Set("Content-Type", "application/json")
		response, err := c.http.Do(request)
		if err != nil {
			lastError = err
		} else {
			var decoded rpcResponse
			decodeError := json.NewDecoder(response.Body).Decode(&decoded)
			closeError := response.Body.Close()
			if response.StatusCode != http.StatusOK {
				lastError = fmt.Errorf(
					"%s returned HTTP %s",
					method,
					response.Status,
				)
			} else if decodeError != nil {
				lastError = fmt.Errorf(
					"decode %s response: %w",
					method,
					decodeError,
				)
			} else if closeError != nil {
				lastError = fmt.Errorf(
					"close %s response: %w",
					method,
					closeError,
				)
			} else if decoded.Error != nil {
				return decoded.Error
			} else if result == nil {
				return nil
			} else if err := json.Unmarshal(decoded.Result, result); err != nil {
				return fmt.Errorf("decode %s result: %w", method, err)
			} else {
				return nil
			}
		}
		if attempt < 4 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt+1) * 250 * time.Millisecond):
			}
		}
	}
	return fmt.Errorf("%s failed after retries: %w", method, lastError)
}

type rpcBlock struct {
	Number    string `json:"number"`
	Hash      string `json:"hash"`
	StateRoot string `json:"stateRoot"`
}

type rpcLog struct {
	Address     string   `json:"address"`
	Topics      []string `json:"topics"`
	Data        string   `json:"data"`
	BlockNumber string   `json:"blockNumber"`
	Removed     bool     `json:"removed"`
}

type eventCounts struct {
	Deposits    uint64
	Withdrawals uint64
	Transfers   uint64
}

func (c *eventCounts) add(other eventCounts) {
	c.Deposits += other.Deposits
	c.Withdrawals += other.Withdrawals
	c.Transfers += other.Transfers
}

func (c eventCounts) total() uint64 {
	return c.Deposits + c.Withdrawals + c.Transfers
}

type rangeJob struct {
	from uint64
	to   uint64
}

type rangeResult struct {
	deltas map[common.Address]*big.Int
	counts eventCounts
}

type scanStats struct {
	queries atomic.Uint64
	splits  atomic.Uint64
}

type summary struct {
	SchemaVersion             int    `json:"schema_version"`
	Status                    string `json:"status"`
	SourceKind                string `json:"source_kind"`
	RPC                       string `json:"rpc"`
	ContractAddress           string `json:"contract_address"`
	ContractCodeHash          string `json:"contract_code_hash"`
	BalanceMappingSlot        uint64 `json:"balance_mapping_slot"`
	CutoffBlock               uint64 `json:"cutoff_block"`
	CutoffBlockHash           string `json:"cutoff_block_hash"`
	CutoffStateRoot           string `json:"cutoff_state_root"`
	DeploymentBlock           uint64 `json:"deployment_block"`
	RangeSize                 uint64 `json:"range_size"`
	InitialRanges             uint64 `json:"initial_ranges"`
	RPCLogQueries             uint64 `json:"rpc_log_queries"`
	AdaptiveRangeSplits       uint64 `json:"adaptive_range_splits"`
	DepositEvents             uint64 `json:"deposit_events"`
	WithdrawalEvents          uint64 `json:"withdrawal_events"`
	TransferEvents            uint64 `json:"transfer_events"`
	HolderCount               uint64 `json:"holder_count"`
	TotalHolderBalanceAtto    string `json:"total_holder_balance_atto"`
	ContractTotalSupplyAtto   string `json:"contract_total_supply_atto"`
	ContractNativeReserveAtto string `json:"contract_native_reserve_atto"`
	ReserveMinusSupplyAtto    string `json:"reserve_minus_supply_atto"`
	OutputPath                string `json:"output_path"`
	OutputSHA256              string `json:"output_sha256"`
	ElapsedMilliseconds       int64  `json:"elapsed_milliseconds"`
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseUint64Quantity(value string) (uint64, error) {
	if !strings.HasPrefix(value, "0x") {
		return 0, fmt.Errorf("quantity lacks 0x prefix: %q", value)
	}
	return strconv.ParseUint(strings.TrimPrefix(value, "0x"), 16, 64)
}

func parseBigQuantity(value string) (*big.Int, error) {
	if !strings.HasPrefix(value, "0x") {
		return nil, fmt.Errorf("quantity lacks 0x prefix: %q", value)
	}
	raw := strings.TrimPrefix(value, "0x")
	if raw == "" {
		return nil, errors.New("empty hexadecimal quantity")
	}
	result, ok := new(big.Int).SetString(raw, 16)
	if !ok {
		return nil, fmt.Errorf("invalid hexadecimal quantity %q", value)
	}
	return result, nil
}

func hexQuantity(value uint64) string {
	return fmt.Sprintf("0x%x", value)
}

func parseHexBytes(value string) ([]byte, error) {
	if !strings.HasPrefix(value, "0x") {
		return nil, fmt.Errorf("hex data lacks 0x prefix")
	}
	raw := strings.TrimPrefix(value, "0x")
	if len(raw)%2 != 0 {
		return nil, fmt.Errorf("hex data has odd length")
	}
	result, err := hex.DecodeString(raw)
	if err != nil {
		return nil, fmt.Errorf("decode hex data: %w", err)
	}
	return result, nil
}

func contractCodeAt(
	ctx context.Context,
	client *rpcClient,
	contract common.Address,
	block uint64,
) ([]byte, error) {
	var result string
	if err := client.call(
		ctx,
		"eth_getCode",
		[]any{contract.Hex(), hexQuantity(block)},
		&result,
	); err != nil {
		return nil, err
	}
	return parseHexBytes(result)
}

func findDeploymentBlock(
	ctx context.Context,
	client *rpcClient,
	contract common.Address,
	cutoff uint64,
) (uint64, error) {
	code, err := contractCodeAt(ctx, client, contract, cutoff)
	if err != nil {
		return 0, fmt.Errorf("read cutoff contract code: %w", err)
	}
	if len(code) == 0 {
		return 0, errors.New("contract has no code at cutoff")
	}
	code, err = contractCodeAt(ctx, client, contract, 0)
	if err != nil {
		return 0, fmt.Errorf("read genesis contract code: %w", err)
	}
	if len(code) != 0 {
		return 0, nil
	}
	low, high := uint64(1), cutoff
	for low < high {
		middle := low + (high-low)/2
		code, err := contractCodeAt(ctx, client, contract, middle)
		if err != nil {
			return 0, fmt.Errorf(
				"read contract code at block %d: %w",
				middle,
				err,
			)
		}
		if len(code) == 0 {
			low = middle + 1
		} else {
			high = middle
		}
	}
	return low, nil
}

func fetchLogs(
	ctx context.Context,
	client *rpcClient,
	contract common.Address,
	from uint64,
	to uint64,
	stats *scanStats,
) ([]rpcLog, error) {
	stats.queries.Add(1)
	var logs []rpcLog
	err := client.call(
		ctx,
		"eth_getLogs",
		[]any{map[string]any{
			"fromBlock": hexQuantity(from),
			"toBlock":   hexQuantity(to),
			"address":   contract.Hex(),
			"topics": []any{
				[]string{depositTopic, withdrawTopic, transferTopic},
			},
		}},
		&logs,
	)
	if err == nil {
		return logs, nil
	}
	if from == to {
		return nil, fmt.Errorf("fetch logs at block %d: %w", from, err)
	}
	stats.splits.Add(1)
	middle := from + (to-from)/2
	left, leftError := fetchLogs(
		ctx,
		client,
		contract,
		from,
		middle,
		stats,
	)
	if leftError != nil {
		return nil, leftError
	}
	right, rightError := fetchLogs(
		ctx,
		client,
		contract,
		middle+1,
		to,
		stats,
	)
	if rightError != nil {
		return nil, rightError
	}
	return append(left, right...), nil
}

func topicAddress(value string) (common.Address, error) {
	decoded, err := parseHexBytes(value)
	if err != nil {
		return common.Address{}, err
	}
	if len(decoded) != common.HashLength {
		return common.Address{}, fmt.Errorf(
			"address topic has %d bytes",
			len(decoded),
		)
	}
	for _, value := range decoded[:common.HashLength-common.AddressLength] {
		if value != 0 {
			return common.Address{}, errors.New(
				"address topic has a nonzero high byte",
			)
		}
	}
	return common.BytesToAddress(decoded[12:]), nil
}

func logAmount(value string) (*big.Int, error) {
	decoded, err := parseHexBytes(value)
	if err != nil {
		return nil, err
	}
	if len(decoded) != common.HashLength {
		return nil, fmt.Errorf("event data has %d bytes", len(decoded))
	}
	return new(big.Int).SetBytes(decoded), nil
}

func addDelta(
	deltas map[common.Address]*big.Int,
	address common.Address,
	amount *big.Int,
) {
	if address == zeroAddress || amount.Sign() == 0 {
		return
	}
	current := deltas[address]
	if current == nil {
		current = new(big.Int)
		deltas[address] = current
	}
	current.Add(current, amount)
}

func applyLog(
	log rpcLog,
	contract common.Address,
	deltas map[common.Address]*big.Int,
	counts *eventCounts,
) error {
	if log.Removed {
		return errors.New("archival RPC returned a removed log")
	}
	if !strings.EqualFold(log.Address, contract.Hex()) {
		return fmt.Errorf("unexpected log address %q", log.Address)
	}
	if _, err := parseUint64Quantity(log.BlockNumber); err != nil {
		return fmt.Errorf("invalid log block number: %w", err)
	}
	if len(log.Topics) == 0 {
		return errors.New("event log has no topics")
	}
	amount, err := logAmount(log.Data)
	if err != nil {
		return fmt.Errorf("decode event amount: %w", err)
	}

	switch strings.ToLower(log.Topics[0]) {
	case depositTopic:
		if len(log.Topics) != 2 {
			return fmt.Errorf("deposit event has %d topics", len(log.Topics))
		}
		address, err := topicAddress(log.Topics[1])
		if err != nil {
			return fmt.Errorf("decode deposit address: %w", err)
		}
		addDelta(deltas, address, amount)
		counts.Deposits++
	case withdrawTopic:
		if len(log.Topics) != 2 {
			return fmt.Errorf(
				"withdrawal event has %d topics",
				len(log.Topics),
			)
		}
		address, err := topicAddress(log.Topics[1])
		if err != nil {
			return fmt.Errorf("decode withdrawal address: %w", err)
		}
		addDelta(deltas, address, new(big.Int).Neg(amount))
		counts.Withdrawals++
	case transferTopic:
		if len(log.Topics) != 3 {
			return fmt.Errorf("transfer event has %d topics", len(log.Topics))
		}
		source, err := topicAddress(log.Topics[1])
		if err != nil {
			return fmt.Errorf("decode transfer source: %w", err)
		}
		destination, err := topicAddress(log.Topics[2])
		if err != nil {
			return fmt.Errorf("decode transfer destination: %w", err)
		}
		addDelta(deltas, source, new(big.Int).Neg(amount))
		addDelta(deltas, destination, amount)
		counts.Transfers++
	default:
		return fmt.Errorf("unexpected event signature %q", log.Topics[0])
	}
	return nil
}

func scanRanges(
	ctx context.Context,
	client *rpcClient,
	contract common.Address,
	deployment uint64,
	cutoff uint64,
	rangeSize uint64,
	workers int,
) (
	map[common.Address]*big.Int,
	eventCounts,
	uint64,
	scanStats,
	error,
) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	jobs := make(chan rangeJob, workers*2)
	results := make(chan rangeResult, workers*2)
	errorChannel := make(chan error, 1)
	var workerGroup sync.WaitGroup
	stats := scanStats{}

	for worker := 0; worker < workers; worker++ {
		workerGroup.Add(1)
		go func() {
			defer workerGroup.Done()
			for job := range jobs {
				logs, err := fetchLogs(
					ctx,
					client,
					contract,
					job.from,
					job.to,
					&stats,
				)
				if err != nil {
					select {
					case errorChannel <- err:
					default:
					}
					cancel()
					return
				}
				result := rangeResult{
					deltas: make(map[common.Address]*big.Int),
				}
				for _, log := range logs {
					if err := applyLog(
						log,
						contract,
						result.deltas,
						&result.counts,
					); err != nil {
						select {
						case errorChannel <- err:
						default:
						}
						cancel()
						return
					}
				}
				select {
				case results <- result:
				case <-ctx.Done():
					return
				}
			}
		}()
	}

	initialRanges := (cutoff-deployment)/rangeSize + 1
	go func() {
		defer close(jobs)
		for from := deployment; from <= cutoff; {
			to := from + rangeSize - 1
			if to < from || to > cutoff {
				to = cutoff
			}
			select {
			case jobs <- rangeJob{from: from, to: to}:
			case <-ctx.Done():
				return
			}
			if to == cutoff {
				return
			}
			from = to + 1
		}
	}()
	go func() {
		workerGroup.Wait()
		close(results)
	}()

	balances := make(map[common.Address]*big.Int)
	counts := eventCounts{}
	var completed uint64
	started := time.Now()
	lastProgress := started
	for result := range results {
		for address, delta := range result.deltas {
			current := balances[address]
			if current == nil {
				current = new(big.Int)
				balances[address] = current
			}
			current.Add(current, delta)
		}
		counts.add(result.counts)
		completed++
		if time.Since(lastProgress) >= 10*time.Second {
			fmt.Fprintf(
				os.Stderr,
				"progress ranges=%d/%d queries=%d splits=%d events=%d addresses=%d elapsed=%s\n",
				completed,
				initialRanges,
				stats.queries.Load(),
				stats.splits.Load(),
				counts.total(),
				len(balances),
				time.Since(started).Round(time.Second),
			)
			lastProgress = time.Now()
		}
	}
	select {
	case err := <-errorChannel:
		return nil, eventCounts{}, initialRanges, stats, err
	default:
	}
	if completed != initialRanges {
		return nil, eventCounts{}, initialRanges, stats, fmt.Errorf(
			"completed %d of %d ranges",
			completed,
			initialRanges,
		)
	}
	return balances, counts, initialRanges, stats, nil
}

func formatToken(value *big.Int) string {
	divisor := new(big.Int).Exp(big.NewInt(10), big.NewInt(18), nil)
	whole, fraction := new(big.Int), new(big.Int)
	whole.QuoRem(value, divisor, fraction)
	fractionText := fraction.String()
	return fmt.Sprintf(
		"%s.%s%s",
		whole.String(),
		strings.Repeat("0", 18-len(fractionText)),
		fractionText,
	)
}

func writeCSV(
	path string,
	balances map[common.Address]*big.Int,
	expectedTotal *big.Int,
) (string, uint64, *big.Int, error) {
	addresses := make([]common.Address, 0, len(balances))
	total := new(big.Int)
	for address, balance := range balances {
		if balance.Sign() < 0 {
			return "", 0, nil, fmt.Errorf(
				"negative final balance for %s: %s",
				address.Hex(),
				balance,
			)
		}
		if balance.Sign() == 0 {
			continue
		}
		addresses = append(addresses, address)
		total.Add(total, balance)
	}
	if total.Cmp(expectedTotal) != 0 {
		return "", 0, nil, fmt.Errorf(
			"event-derived holder total %s does not equal expected total %s",
			total,
			expectedTotal,
		)
	}
	sort.Slice(addresses, func(i, j int) bool {
		return bytes.Compare(addresses[i][:], addresses[j][:]) < 0
	})

	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o700); err != nil {
		return "", 0, nil, fmt.Errorf("create output directory: %w", err)
	}
	partial := path + ".partial"
	output, err := os.OpenFile(
		partial,
		os.O_CREATE|os.O_EXCL|os.O_WRONLY,
		0o600,
	)
	if err != nil {
		return "", 0, nil, fmt.Errorf("create partial CSV: %w", err)
	}
	complete := false
	defer func() {
		output.Close()
		if !complete {
			os.Remove(partial)
		}
	}()

	digest := sha256.New()
	buffer := bufio.NewWriterSize(
		io.MultiWriter(output, digest),
		4*1024*1024,
	)
	writer := csv.NewWriter(buffer)
	if err := writer.Write([]string{
		"address",
		"wone_balance_atto",
		"wone_balance",
	}); err != nil {
		return "", 0, nil, err
	}
	for _, address := range addresses {
		balance := balances[address]
		if err := writer.Write([]string{
			address.Hex(),
			balance.String(),
			formatToken(balance),
		}); err != nil {
			return "", 0, nil, err
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		return "", 0, nil, fmt.Errorf("flush CSV encoder: %w", err)
	}
	if err := buffer.Flush(); err != nil {
		return "", 0, nil, fmt.Errorf("flush CSV buffer: %w", err)
	}
	if err := output.Sync(); err != nil {
		return "", 0, nil, fmt.Errorf("sync CSV: %w", err)
	}
	if err := output.Close(); err != nil {
		return "", 0, nil, fmt.Errorf("close CSV: %w", err)
	}
	if err := os.Rename(partial, path); err != nil {
		return "", 0, nil, fmt.Errorf("publish CSV: %w", err)
	}
	complete = true
	return hex.EncodeToString(digest.Sum(nil)),
		uint64(len(addresses)),
		total,
		nil
}

func writeJSON(path string, value any) error {
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o700); err != nil {
		return fmt.Errorf("create summary directory: %w", err)
	}
	partial := path + ".partial"
	output, err := os.OpenFile(
		partial,
		os.O_CREATE|os.O_EXCL|os.O_WRONLY,
		0o600,
	)
	if err != nil {
		return fmt.Errorf("create partial summary: %w", err)
	}
	encoder := json.NewEncoder(output)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		output.Close()
		os.Remove(partial)
		return fmt.Errorf("encode summary: %w", err)
	}
	if err := output.Sync(); err != nil {
		output.Close()
		os.Remove(partial)
		return fmt.Errorf("sync summary: %w", err)
	}
	if err := output.Close(); err != nil {
		os.Remove(partial)
		return fmt.Errorf("close summary: %w", err)
	}
	if err := os.Rename(partial, path); err != nil {
		os.Remove(partial)
		return fmt.Errorf("publish summary: %w", err)
	}
	return nil
}

func main() {
	var (
		rpcURL = flag.String(
			"rpc",
			"",
			"Harmony archival shard-0 JSON-RPC URL",
		)
		contractText = flag.String(
			"contract",
			"0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",
			"WONE contract address",
		)
		cutoff       = flag.Uint64("cutoff-block", 0, "inclusive cutoff block")
		expectedHash = flag.String(
			"cutoff-hash",
			"",
			"expected cutoff block hash",
		)
		expectedRoot = flag.String(
			"cutoff-state-root",
			"",
			"expected cutoff state root",
		)
		expectedCodeHash = flag.String(
			"code-hash",
			"",
			"expected WONE runtime code hash",
		)
		rangeSize = flag.Uint64(
			"range-size",
			1024,
			"initial inclusive eth_getLogs range size",
		)
		workers     = flag.Int("workers", 32, "concurrent RPC range workers")
		outputPath  = flag.String("output", "", "holder CSV output")
		summaryPath = flag.String("summary", "", "summary JSON output")
	)
	flag.Parse()
	if *rpcURL == "" ||
		*cutoff == 0 ||
		*expectedHash == "" ||
		*expectedRoot == "" ||
		*expectedCodeHash == "" ||
		*rangeSize == 0 ||
		*workers <= 0 ||
		*outputPath == "" ||
		*summaryPath == "" {
		flag.Usage()
		os.Exit(2)
	}
	if !common.IsHexAddress(*contractText) {
		fatalf("invalid contract address %q", *contractText)
	}
	contract := common.HexToAddress(*contractText)
	ctx := context.Background()
	client := newRPCClient(*rpcURL)
	started := time.Now()

	var block rpcBlock
	if err := client.call(
		ctx,
		"eth_getBlockByNumber",
		[]any{hexQuantity(*cutoff), false},
		&block,
	); err != nil {
		fatalf("read cutoff block: %v", err)
	}
	blockNumber, err := parseUint64Quantity(block.Number)
	if err != nil {
		fatalf("decode cutoff block number: %v", err)
	}
	if blockNumber != *cutoff {
		fatalf("RPC returned block %d, expected %d", blockNumber, *cutoff)
	}
	if !strings.EqualFold(block.Hash, *expectedHash) {
		fatalf(
			"cutoff block hash mismatch: got %s want %s",
			block.Hash,
			*expectedHash,
		)
	}
	if !strings.EqualFold(block.StateRoot, *expectedRoot) {
		fatalf(
			"cutoff state root mismatch: got %s want %s",
			block.StateRoot,
			*expectedRoot,
		)
	}

	code, err := contractCodeAt(ctx, client, contract, *cutoff)
	if err != nil {
		fatalf("read cutoff contract code: %v", err)
	}
	codeHash := crypto.Keccak256Hash(code)
	if !strings.EqualFold(codeHash.Hex(), *expectedCodeHash) {
		fatalf(
			"WONE code hash mismatch: got %s want %s",
			codeHash.Hex(),
			*expectedCodeHash,
		)
	}
	deployment, err := findDeploymentBlock(
		ctx,
		client,
		contract,
		*cutoff,
	)
	if err != nil {
		fatalf("find WONE deployment block: %v", err)
	}
	fmt.Fprintf(os.Stderr, "deployment block=%d\n", deployment)

	balances, counts, initialRanges, stats, err := scanRanges(
		ctx,
		client,
		contract,
		deployment,
		*cutoff,
		*rangeSize,
		*workers,
	)
	if err != nil {
		fatalf("scan WONE events: %v", err)
	}

	var totalSupplyText string
	if err := client.call(
		ctx,
		"eth_call",
		[]any{
			map[string]string{
				"to":   contract.Hex(),
				"data": totalSupplyCall,
			},
			hexQuantity(*cutoff),
		},
		&totalSupplyText,
	); err != nil {
		fatalf("read WONE totalSupply: %v", err)
	}
	totalSupply, err := parseBigQuantity(totalSupplyText)
	if err != nil {
		fatalf("decode WONE totalSupply: %v", err)
	}
	var reserveText string
	if err := client.call(
		ctx,
		"eth_getBalance",
		[]any{contract.Hex(), hexQuantity(*cutoff)},
		&reserveText,
	); err != nil {
		fatalf("read WONE native reserve: %v", err)
	}
	reserve, err := parseBigQuantity(reserveText)
	if err != nil {
		fatalf("decode WONE native reserve: %v", err)
	}

	outputHash, holderCount, holderTotal, err := writeCSV(
		*outputPath,
		balances,
		totalSupply,
	)
	if err != nil {
		fatalf("write holder CSV: %v", err)
	}
	reserveMinusSupply := new(big.Int).Sub(
		new(big.Int).Set(reserve),
		totalSupply,
	)
	if reserveMinusSupply.Sign() != 0 {
		fatalf(
			"native reserve %s does not equal totalSupply %s",
			reserve,
			totalSupply,
		)
	}

	result := summary{
		SchemaVersion:             1,
		Status:                    "passed",
		SourceKind:                "Harmony archival-node eth_getLogs event replay plus cutoff eth_call and eth_getBalance; no explorer data",
		RPC:                       *rpcURL,
		ContractAddress:           contract.Hex(),
		ContractCodeHash:          codeHash.Hex(),
		BalanceMappingSlot:        3,
		CutoffBlock:               *cutoff,
		CutoffBlockHash:           block.Hash,
		CutoffStateRoot:           block.StateRoot,
		DeploymentBlock:           deployment,
		RangeSize:                 *rangeSize,
		InitialRanges:             initialRanges,
		RPCLogQueries:             stats.queries.Load(),
		AdaptiveRangeSplits:       stats.splits.Load(),
		DepositEvents:             counts.Deposits,
		WithdrawalEvents:          counts.Withdrawals,
		TransferEvents:            counts.Transfers,
		HolderCount:               holderCount,
		TotalHolderBalanceAtto:    holderTotal.String(),
		ContractTotalSupplyAtto:   totalSupply.String(),
		ContractNativeReserveAtto: reserve.String(),
		ReserveMinusSupplyAtto:    reserveMinusSupply.String(),
		OutputPath:                *outputPath,
		OutputSHA256:              outputHash,
		ElapsedMilliseconds:       time.Since(started).Milliseconds(),
	}
	if err := writeJSON(*summaryPath, result); err != nil {
		fatalf("write summary: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("print summary: %v", err)
	}
}
