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
	"math/big"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	ethtypes "github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
	"github.com/harmony-one/harmony/core/rawdb"
)

type fileList []string

func (values *fileList) String() string {
	return strings.Join(*values, ",")
}

func (values *fileList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

type target struct {
	key      common.Hash
	balance  *big.Int
	nonce    uint64
	codeHash common.Hash
}

type outputRow struct {
	key                 common.Hash
	transitionBlock     uint64
	previousBalance     *big.Int
	currentBalance      *big.Int
	previousLeafSHA256  string
	currentLeafSHA256   string
	exponentialLookups  uint64
	binarySearchLookups uint64
}

type summary struct {
	DBPath                string   `json:"db_path"`
	TargetsPath           string   `json:"targets_path"`
	MatchPaths            []string `json:"match_paths"`
	OutputPath            string   `json:"output_path"`
	OutputSHA256          string   `json:"output_sha256"`
	ShardID               uint32   `json:"shard_id"`
	CurrentBlock          uint64   `json:"current_block"`
	InputUnresolved       uint64   `json:"input_unresolved"`
	SkippedMatched        uint64   `json:"skipped_matched"`
	SkippedNonTargetShard uint64   `json:"skipped_non_target_shard"`
	PreviouslyLocated     uint64   `json:"previously_located"`
	NewlyLocated          uint64   `json:"newly_located"`
	StateLookups          uint64   `json:"state_lookups"`
	UniqueBlockRootsRead  uint64   `json:"unique_block_roots_read"`
	ElapsedMilliseconds   int64    `json:"elapsed_milliseconds"`
}

type locator struct {
	db           ethdb.Database
	trieDB       *trie.Database
	rootCache    map[uint64]common.Hash
	stateLookups uint64
}

var outputHeader = []string{
	"secure_key",
	"transition_block",
	"previous_block",
	"previous_balance_atto",
	"current_balance_atto",
	"previous_leaf_sha256",
	"current_leaf_sha256",
	"exponential_lookups",
	"binary_search_lookups",
	"method",
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

func parseUnsigned(value, field string) (*big.Int, error) {
	result, ok := new(big.Int).SetString(value, 10)
	if !ok || result.Sign() < 0 {
		return nil, fmt.Errorf("invalid %s %q", field, value)
	}
	return result, nil
}

func loadMatched(paths []string) map[common.Hash]struct{} {
	result := make(map[common.Hash]struct{})
	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			fatalf("open match file %s: %v", path, err)
		}
		reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
		header, err := reader.Read()
		if err != nil {
			file.Close()
			fatalf("read match header %s: %v", path, err)
		}
		columns := indexes(header)
		keyColumn, haveKey := columns["secure_key"]
		addressColumn, haveAddress := columns["address"]
		if !haveKey || !haveAddress {
			file.Close()
			fatalf("match CSV %s must contain secure_key and address", path)
		}
		for {
			row, err := reader.Read()
			if err == io.EOF {
				break
			}
			if err != nil {
				file.Close()
				fatalf("read match CSV %s: %v", path, err)
			}
			key, err := parseHash(row[keyColumn])
			if err != nil {
				file.Close()
				fatalf("match CSV %s: %v", path, err)
			}
			if !common.IsHexAddress(row[addressColumn]) {
				file.Close()
				fatalf("match CSV %s has invalid address %q", path, row[addressColumn])
			}
			address := common.HexToAddress(row[addressColumn])
			if crypto.Keccak256Hash(address.Bytes()) != key {
				file.Close()
				fatalf("match CSV %s has a cryptographic mismatch for %s", path, key.Hex())
			}
			result[key] = struct{}{}
		}
		if err := file.Close(); err != nil {
			fatalf("close match CSV %s: %v", path, err)
		}
	}
	return result
}

func loadTargets(path string, matched map[common.Hash]struct{}, shardID uint) ([]target, uint64, uint64, uint64) {
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
	balanceField := fmt.Sprintf("liquid_shard%d_atto", shardID)
	nonceField := fmt.Sprintf("nonce_shard%d", shardID)
	codeHashField := fmt.Sprintf("code_hash_shard%d", shardID)
	required := []string{
		"secure_key",
		"address",
		balanceField,
		nonceField,
		codeHashField,
	}
	for _, name := range required {
		if _, ok := columns[name]; !ok {
			fatalf("target CSV is missing column %s", name)
		}
	}

	var inputUnresolved, skippedMatched, skippedNonShardZero uint64
	var result []target
	for line := 2; ; line++ {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read target row %d: %v", line, err)
		}
		if row[columns["address"]] != "" {
			continue
		}
		inputUnresolved++
		key, err := parseHash(row[columns["secure_key"]])
		if err != nil {
			fatalf("target row %d: %v", line, err)
		}
		if _, ok := matched[key]; ok {
			skippedMatched++
			continue
		}
		balance, err := parseUnsigned(row[columns[balanceField]], balanceField)
		if err != nil {
			fatalf("target row %d: %v", line, err)
		}
		if balance.Sign() == 0 {
			skippedNonShardZero++
			continue
		}
		nonce, err := strconv.ParseUint(row[columns[nonceField]], 10, 64)
		if err != nil {
			fatalf("target row %d has invalid nonce: %v", line, err)
		}
		codeHash, err := parseHash(row[columns[codeHashField]])
		if err != nil {
			fatalf("target row %d: %v", line, err)
		}
		result = append(result, target{
			key:      key,
			balance:  balance,
			nonce:    nonce,
			codeHash: codeHash,
		})
	}
	return result, inputUnresolved, skippedMatched, skippedNonShardZero
}

func loadLocated(path string) map[common.Hash]struct{} {
	result := make(map[common.Hash]struct{})
	file, err := os.Open(path)
	if os.IsNotExist(err) {
		return result
	}
	if err != nil {
		fatalf("open existing output: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read existing output header: %v", err)
	}
	if !equalStrings(header, outputHeader) {
		fatalf("existing output has an unexpected header")
	}
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read existing output: %v", err)
		}
		key, err := parseHash(row[0])
		if err != nil {
			fatalf("existing output: %v", err)
		}
		result[key] = struct{}{}
	}
	return result
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func openOutput(path string) (*os.File, *csv.Writer) {
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
		if err := writer.Write(outputHeader); err != nil {
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
	return file, writer
}

func (locator *locator) rootAt(blockNumber uint64) common.Hash {
	if root, ok := locator.rootCache[blockNumber]; ok {
		return root
	}
	hash := rawdb.ReadCanonicalHash(locator.db, blockNumber)
	if hash == (common.Hash{}) {
		fatalf("missing canonical hash at block %d", blockNumber)
	}
	header := rawdb.ReadHeader(locator.db, hash, blockNumber)
	if header == nil {
		fatalf("missing canonical header at block %d", blockNumber)
	}
	root := header.Root()
	locator.rootCache[blockNumber] = root
	return root
}

func (locator *locator) leafAt(blockNumber uint64, key common.Hash) []byte {
	stateTrie, err := trie.New(trie.StateTrieID(locator.rootAt(blockNumber)), locator.trieDB)
	if err != nil {
		fatalf("open state trie at block %d: %v", blockNumber, err)
	}
	encoded, err := stateTrie.TryGet(key.Bytes())
	if err != nil {
		fatalf("read account %s at block %d: %v", key.Hex(), blockNumber, err)
	}
	locator.stateLookups++
	return common.CopyBytes(encoded)
}

func decodeAccount(encoded []byte, key common.Hash, blockNumber uint64) ethtypes.StateAccount {
	if len(encoded) == 0 {
		return ethtypes.StateAccount{Balance: new(big.Int)}
	}
	var account ethtypes.StateAccount
	if err := rlp.DecodeBytes(encoded, &account); err != nil {
		fatalf("decode account %s at block %d: %v", key.Hex(), blockNumber, err)
	}
	if account.Balance == nil || account.Balance.Sign() < 0 {
		fatalf("invalid account balance %s at block %d", key.Hex(), blockNumber)
	}
	return account
}

func leafSHA256(encoded []byte) string {
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:])
}

func locateTransition(locator *locator, item target, currentBlock uint64) outputRow {
	currentLeaf := locator.leafAt(currentBlock, item.key)
	if len(currentLeaf) == 0 {
		fatalf("target account %s is absent at current block %d", item.key.Hex(), currentBlock)
	}
	currentAccount := decodeAccount(currentLeaf, item.key, currentBlock)
	if currentAccount.Balance.Cmp(item.balance) != 0 {
		fatalf("target balance mismatch for %s: trie=%s CSV=%s", item.key.Hex(), currentAccount.Balance, item.balance)
	}
	if currentAccount.Nonce != item.nonce {
		fatalf("target nonce mismatch for %s: trie=%d CSV=%d", item.key.Hex(), currentAccount.Nonce, item.nonce)
	}
	if common.BytesToHash(currentAccount.CodeHash) != item.codeHash {
		fatalf("target code hash mismatch for %s", item.key.Hex())
	}
	if currentBlock == 0 {
		return outputRow{
			key:                item.key,
			transitionBlock:    0,
			previousBalance:    new(big.Int),
			currentBalance:     new(big.Int).Set(currentAccount.Balance),
			previousLeafSHA256: leafSHA256(nil),
			currentLeafSHA256:  leafSHA256(currentLeaf),
		}
	}

	upper := currentBlock
	var lower uint64
	var previousLeaf []byte
	var exponentialLookups uint64
	for step := uint64(1); ; {
		probe := uint64(0)
		if step < currentBlock {
			probe = currentBlock - step
		}
		leaf := locator.leafAt(probe, item.key)
		exponentialLookups++
		if !bytes.Equal(leaf, currentLeaf) {
			lower = probe
			previousLeaf = leaf
			break
		}
		upper = probe
		if probe == 0 {
			return outputRow{
				key:                item.key,
				transitionBlock:    0,
				previousBalance:    new(big.Int),
				currentBalance:     new(big.Int).Set(currentAccount.Balance),
				previousLeafSHA256: leafSHA256(nil),
				currentLeafSHA256:  leafSHA256(currentLeaf),
				exponentialLookups: exponentialLookups,
			}
		}
		if step > currentBlock/2 {
			step = currentBlock
		} else {
			step *= 2
		}
	}

	var binaryLookups uint64
	for upper-lower > 1 {
		middle := lower + (upper-lower)/2
		leaf := locator.leafAt(middle, item.key)
		binaryLookups++
		if bytes.Equal(leaf, currentLeaf) {
			upper = middle
		} else {
			lower = middle
			previousLeaf = leaf
		}
	}
	if !bytes.Equal(locator.leafAt(upper, item.key), currentLeaf) {
		fatalf("transition upper-bound validation failed for %s at block %d", item.key.Hex(), upper)
	}
	previousLeaf = locator.leafAt(upper-1, item.key)
	if bytes.Equal(previousLeaf, currentLeaf) {
		fatalf("transition lower-bound validation failed for %s at block %d", item.key.Hex(), upper-1)
	}
	previousAccount := decodeAccount(previousLeaf, item.key, upper-1)
	return outputRow{
		key:                 item.key,
		transitionBlock:     upper,
		previousBalance:     new(big.Int).Set(previousAccount.Balance),
		currentBalance:      new(big.Int).Set(currentAccount.Balance),
		previousLeafSHA256:  leafSHA256(previousLeaf),
		currentLeafSHA256:   leafSHA256(currentLeaf),
		exponentialLookups:  exponentialLookups,
		binarySearchLookups: binaryLookups,
	}
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
	var matchPaths fileList
	var (
		dbPath       = flag.String("db", "", "canonical archive LevelDB")
		targetsPath  = flag.String("targets", "", "claim CSV containing unresolved secure keys")
		outputPath   = flag.String("output", "", "append-only transition CSV")
		summaryPath  = flag.String("summary", "", "summary JSON")
		currentBlock = flag.Uint64("current-block", 0, "block whose state matches the target CSV")
		shardID      = flag.Uint("shard-id", 0, "liquid-state shard to inspect (0 or 1)")
		cacheMB      = flag.Int("cache-mb", 4096, "LevelDB and trie cache in MiB")
		handles      = flag.Int("handles", 4096, "LevelDB open-file handles")
	)
	flag.Var(&matchPaths, "match", "verified address-match CSV to exclude; repeatable")
	flag.Parse()
	if *dbPath == "" || *targetsPath == "" || *outputPath == "" || *summaryPath == "" || *currentBlock == 0 {
		flag.Usage()
		os.Exit(2)
	}
	if *shardID > 1 {
		fatalf("shard-id must be 0 or 1")
	}

	started := time.Now()
	matched := loadMatched(matchPaths)
	targets, inputUnresolved, skippedMatched, skippedNonTargetShard := loadTargets(*targetsPath, matched, *shardID)
	located := loadLocated(*outputPath)
	previouslyLocated := uint64(len(located))
	outputFile, writer := openOutput(*outputPath)

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		outputFile.Close()
		fatalf("open archive database: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	stateLocator := &locator{
		db:        db,
		trieDB:    trie.NewDatabase(db),
		rootCache: make(map[uint64]common.Hash),
	}

	var newlyLocated uint64
	for _, item := range targets {
		if _, done := located[item.key]; done {
			continue
		}
		result := locateTransition(stateLocator, item, *currentBlock)
		previousBlock := uint64(0)
		if result.transitionBlock > 0 {
			previousBlock = result.transitionBlock - 1
		}
		row := []string{
			result.key.Hex(),
			strconv.FormatUint(result.transitionBlock, 10),
			strconv.FormatUint(previousBlock, 10),
			result.previousBalance.String(),
			result.currentBalance.String(),
			result.previousLeafSHA256,
			result.currentLeafSHA256,
			strconv.FormatUint(result.exponentialLookups, 10),
			strconv.FormatUint(result.binarySearchLookups, 10),
			"exact_current_leaf_suffix_boundary",
		}
		if err := writer.Write(row); err != nil {
			db.Close()
			outputFile.Close()
			fatalf("write transition: %v", err)
		}
		writer.Flush()
		if err := writer.Error(); err != nil {
			db.Close()
			outputFile.Close()
			fatalf("flush transition: %v", err)
		}
		if err := outputFile.Sync(); err != nil {
			db.Close()
			outputFile.Close()
			fatalf("sync transition: %v", err)
		}
		newlyLocated++
		if newlyLocated%100 == 0 {
			fmt.Fprintf(os.Stderr, "progress located=%d/%d state_lookups=%d block_roots=%d\n",
				newlyLocated, len(targets)-len(located), stateLocator.stateLookups, len(stateLocator.rootCache))
		}
	}
	if err := db.Close(); err != nil {
		outputFile.Close()
		fatalf("close archive database: %v", err)
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
		DBPath:                *dbPath,
		TargetsPath:           *targetsPath,
		MatchPaths:            matchPaths,
		OutputPath:            *outputPath,
		OutputSHA256:          fileSHA256(*outputPath),
		ShardID:               uint32(*shardID),
		CurrentBlock:          *currentBlock,
		InputUnresolved:       inputUnresolved,
		SkippedMatched:        skippedMatched,
		SkippedNonTargetShard: skippedNonTargetShard,
		PreviouslyLocated:     previouslyLocated,
		NewlyLocated:          newlyLocated,
		StateLookups:          stateLocator.stateLookups,
		UniqueBlockRootsRead:  uint64(len(stateLocator.rootCache)),
		ElapsedMilliseconds:   time.Since(started).Milliseconds(),
	}
	writeJSONAtomic(*summaryPath, result)
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary output: %v", err)
	}
}
