package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/big"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/btcsuite/btcutil/bech32"
	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	gethleveldb "github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/harmony-one/harmony/block"
	"github.com/harmony-one/harmony/core/rawdb"
	hmytypes "github.com/harmony-one/harmony/core/types"
	staking "github.com/harmony-one/harmony/staking/types"
	nativeleveldb "github.com/syndtr/goleveldb/leveldb"
	"github.com/syndtr/goleveldb/leveldb/opt"
	"github.com/syndtr/goleveldb/leveldb/util"
)

const attoPerONE = "1000000000000000000"

var outputHeader = []string{
	"secure_key",
	"address",
	"last_activity_time_utc",
	"last_activity_timestamp_unix",
	"last_activity_block",
	"last_activity_shard",
	"last_activity_type",
	"last_activity_tx_hash",
	"last_activity_index",
	"last_activity_detail",
}

type candidate struct {
	SecureKey common.Hash
	Address   common.Address
}

type activity struct {
	TimeUTC       string
	TimestampUnix uint64
	Block         uint64
	Shard         uint32
	Type          string
	TxHash        string
	Index         uint64
	Detail        string
}

type counters struct {
	TransactionLookupEntries uint64 `json:"transaction_lookup_entries"`
	TransactionBlocks        uint64 `json:"transaction_blocks"`
	CanonicalBlocksRead      uint64 `json:"canonical_transaction_blocks_read"`
	RegularTransactions      uint64 `json:"regular_transactions"`
	StakingTransactions      uint64 `json:"staking_transactions"`
	StaleIndexEntries        uint64 `json:"stale_index_entries"`
	CandidateChecks          uint64 `json:"candidate_checks"`
}

type summary struct {
	Status               string   `json:"status"`
	SourceKind           string   `json:"source_kind"`
	DBPath               string   `json:"db_path"`
	ExplorerDBPath       string   `json:"explorer_db_path,omitempty"`
	CandidatesPath       string   `json:"candidates_path"`
	CandidatesSHA256     string   `json:"candidates_sha256"`
	Candidates           uint64   `json:"candidates"`
	Shard                uint32   `json:"shard"`
	CutoffBlock          uint64   `json:"cutoff_block"`
	CutoffHash           string   `json:"cutoff_hash"`
	TransactionIndexTail *uint64  `json:"transaction_index_tail"`
	ActivityFound        uint64   `json:"activity_found"`
	ActivityNotFound     uint64   `json:"activity_not_found"`
	Counters             counters `json:"counters"`
	OutputPath           string   `json:"output_path"`
	OutputSHA256         string   `json:"output_sha256"`
	ElapsedMilliseconds  int64    `json:"elapsed_milliseconds"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func encodeBlockNumber(number uint64) []byte {
	result := make([]byte, 8)
	binary.BigEndian.PutUint64(result, number)
	return result
}

func canonicalHashKey(number uint64) []byte {
	key := append([]byte{'h'}, encodeBlockNumber(number)...)
	return append(key, 'n')
}

func bodyKey(number uint64, hash common.Hash) []byte {
	key := append([]byte{'b'}, encodeBlockNumber(number)...)
	return append(key, hash.Bytes()...)
}

func headerKey(number uint64, hash common.Hash) []byte {
	key := append([]byte{'h'}, encodeBlockNumber(number)...)
	return append(key, hash.Bytes()...)
}

func fileSHA256(path string) string {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open %s for hashing: %v", path, err)
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		fatalf("hash %s: %v", path, err)
	}
	return hex.EncodeToString(hasher.Sum(nil))
}

func columns(header []string) map[string]int {
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

func loadCandidates(
	path string,
	shard uint32,
	cutoff uint64,
) ([]candidate, map[common.Address]int) {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open candidates: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read candidate header: %v", err)
	}
	indexes := columns(header)
	blockField := fmt.Sprintf("claims_shard%d_block", shard)
	for _, name := range []string{
		"secure_key",
		"address",
		blockField,
		"total_claim_atto",
	} {
		if _, exists := indexes[name]; !exists {
			fatalf("candidate CSV is missing %s", name)
		}
	}
	minimum := new(big.Int)
	minimum.SetString(attoPerONE, 10)
	minimum.Mul(minimum, big.NewInt(1000))
	var (
		result   []candidate
		previous common.Hash
		havePrev bool
	)
	byAddress := make(map[common.Address]int)
	for line := 2; ; line++ {
		row, readErr := reader.Read()
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			fatalf("read candidate row %d: %v", line, readErr)
		}
		if len(row) != len(header) {
			fatalf("candidate row %d has wrong field count", line)
		}
		secureKey, parseErr := parseHash(row[indexes["secure_key"]])
		if parseErr != nil {
			fatalf("candidate row %d: %v", line, parseErr)
		}
		if havePrev && bytes.Compare(previous[:], secureKey[:]) >= 0 {
			fatalf("secure keys are not increasing at row %d", line)
		}
		previous, havePrev = secureKey, true
		addressText := row[indexes["address"]]
		if !common.IsHexAddress(addressText) {
			fatalf("candidate row %d has invalid address", line)
		}
		address := common.HexToAddress(addressText)
		if crypto.Keccak256Hash(address.Bytes()) != secureKey {
			fatalf("candidate row %d address does not match secure key", line)
		}
		if _, duplicate := byAddress[address]; duplicate {
			fatalf("candidate row %d has duplicate address", line)
		}
		blockNumber, parseErr := strconv.ParseUint(
			row[indexes[blockField]],
			10,
			64,
		)
		if parseErr != nil || blockNumber != cutoff {
			fatalf("candidate row %d has wrong shard cutoff", line)
		}
		total := new(big.Int)
		if _, ok := total.SetString(
			row[indexes["total_claim_atto"]],
			10,
		); !ok || total.Cmp(minimum) < 0 {
			fatalf("candidate row %d has invalid or low total claim", line)
		}
		byAddress[address] = len(result)
		result = append(result, candidate{
			SecureKey: secureKey,
			Address:   address,
		})
	}
	return result, byAddress
}

func readRequired(db ethdb.Database, key []byte, label string) []byte {
	exists, err := db.Has(key)
	if err != nil {
		fatalf("check %s: %v", label, err)
	}
	if !exists {
		fatalf("missing %s", label)
	}
	value, err := db.Get(key)
	if err != nil {
		fatalf("read %s: %v", label, err)
	}
	return value
}

func readCanonicalHash(db ethdb.Database, number uint64) common.Hash {
	value := readRequired(
		db,
		canonicalHashKey(number),
		fmt.Sprintf("canonical hash at block %d", number),
	)
	if len(value) != common.HashLength {
		fatalf("canonical hash at block %d has wrong length", number)
	}
	return common.BytesToHash(value)
}

func readBody(
	db ethdb.Database,
	number uint64,
	hash common.Hash,
) *hmytypes.Body {
	value := readRequired(
		db,
		bodyKey(number, hash),
		fmt.Sprintf("canonical body at block %d", number),
	)
	body := new(hmytypes.Body)
	if err := rlp.DecodeBytes(value, body); err != nil {
		fatalf("decode canonical body at block %d: %v", number, err)
	}
	return body
}

func readTimestamp(
	db ethdb.Database,
	number uint64,
	hash common.Hash,
) uint64 {
	value := readRequired(
		db,
		headerKey(number, hash),
		fmt.Sprintf("canonical header at block %d", number),
	)
	header := new(block.Header)
	if err := rlp.DecodeBytes(value, header); err != nil {
		fatalf("decode canonical header at block %d: %v", number, err)
	}
	if header.Hash() != hash || header.Number().Uint64() != number {
		fatalf("canonical header identity mismatch at block %d", number)
	}
	return header.Time().Uint64()
}

func decodeLookupBlock(value []byte) (uint64, error) {
	var entry rawdb.TxLookupEntry
	if err := rlp.DecodeBytes(value, &entry); err == nil &&
		entry.BlockHash != (common.Hash{}) {
		return entry.BlockIndex, nil
	}
	if len(value) == 0 || len(value) > 8 {
		return 0, fmt.Errorf("invalid lookup value length %d", len(value))
	}
	return new(big.Int).SetBytes(value).Uint64(), nil
}

func setBlock(bitset []uint64, number uint64) bool {
	word, mask := number/64, uint64(1)<<(number%64)
	existed := bitset[word]&mask != 0
	bitset[word] |= mask
	return !existed
}

func hasBlock(bitset []uint64, number uint64) bool {
	return bitset[number/64]&(uint64(1)<<(number%64)) != 0
}

func transactionBlocks(
	db ethdb.Database,
	cutoff uint64,
) ([]uint64, uint64, uint64) {
	bitset := make([]uint64, cutoff/64+1)
	iterator := db.NewIterator([]byte{'l'}, nil)
	var entries, blocks uint64
	for iterator.Next() {
		key := iterator.Key()
		if len(key) == 0 || key[0] != 'l' {
			break
		}
		if len(key) != 1+common.HashLength {
			continue
		}
		number, err := decodeLookupBlock(iterator.Value())
		if err != nil {
			iterator.Release()
			fatalf("decode transaction lookup %x: %v", key, err)
		}
		entries++
		if number <= cutoff && setBlock(bitset, number) {
			blocks++
		}
	}
	if err := iterator.Error(); err != nil {
		iterator.Release()
		fatalf("iterate transaction lookup entries: %v", err)
	}
	iterator.Release()
	return bitset, entries, blocks
}

func transactionIndexTail(db ethdb.Database) *uint64 {
	key := []byte("TransactionIndexTail")
	exists, err := db.Has(key)
	if err != nil {
		fatalf("check transaction index tail: %v", err)
	}
	if !exists {
		return nil
	}
	value, err := db.Get(key)
	if err != nil {
		fatalf("read transaction index tail: %v", err)
	}
	if len(value) != 8 {
		fatalf("transaction index tail has wrong length")
	}
	number := binary.BigEndian.Uint64(value)
	return &number
}

func eventKey(value activity) string {
	return fmt.Sprintf(
		"%020d/%s/%s/%s",
		value.Index,
		value.Type,
		value.Detail,
		value.TxHash,
	)
}

func consider(
	address common.Address,
	value activity,
	unresolved map[common.Address]int,
	blockMatches map[int]activity,
	counts *counters,
) {
	counts.CandidateChecks++
	index, wanted := unresolved[address]
	if !wanted {
		return
	}
	current, exists := blockMatches[index]
	if !exists || eventKey(value) > eventKey(current) {
		blockMatches[index] = value
	}
}

func scanRegular(
	tx *hmytypes.Transaction,
	index uint64,
	shard uint32,
	unresolved map[common.Address]int,
	blockMatches map[int]activity,
	counts *counters,
) {
	counts.RegularTransactions++
	base := activity{
		Shard:  shard,
		Type:   "regular",
		TxHash: tx.HashByType().Hex(),
		Index:  index,
	}
	sender, err := tx.SenderAddress()
	if err != nil {
		fatalf("recover sender for %s: %v", base.TxHash, err)
	}
	value := base
	value.Detail = "sender"
	consider(sender, value, unresolved, blockMatches, counts)
	if recipient := tx.To(); recipient != nil {
		value = base
		value.Detail = "recipient"
		consider(*recipient, value, unresolved, blockMatches, counts)
	} else {
		value = base
		value.Detail = "contract_creation"
		consider(
			crypto.CreateAddress(sender, tx.Nonce()),
			value,
			unresolved,
			blockMatches,
			counts,
		)
	}
}

func scanStaking(
	tx *staking.StakingTransaction,
	index uint64,
	shard uint32,
	unresolved map[common.Address]int,
	blockMatches map[int]activity,
	counts *counters,
) {
	counts.StakingTransactions++
	base := activity{
		Shard:  shard,
		Type:   "staking",
		TxHash: tx.Hash().Hex(),
		Index:  index,
	}
	sender, err := tx.SenderAddress()
	if err != nil {
		fatalf("recover staking sender for %s: %v", base.TxHash, err)
	}
	value := base
	value.Detail = "sender"
	consider(sender, value, unresolved, blockMatches, counts)
	add := func(address common.Address, detail string) {
		item := base
		item.Detail = detail
		consider(address, item, unresolved, blockMatches, counts)
	}
	switch message := tx.StakingMessage().(type) {
	case *staking.CreateValidator:
		add(message.ValidatorAddress, "create_validator")
	case *staking.EditValidator:
		add(message.ValidatorAddress, "edit_validator")
	case *staking.Delegate:
		add(message.DelegatorAddress, "delegate_delegator")
		add(message.ValidatorAddress, "delegate_validator")
	case *staking.Undelegate:
		add(message.DelegatorAddress, "undelegate_delegator")
		add(message.ValidatorAddress, "undelegate_validator")
	case *staking.CollectRewards:
		add(message.DelegatorAddress, "collect_rewards")
	}
}

func oneAddress(address common.Address) string {
	converted, err := bech32.ConvertBits(address.Bytes(), 8, 5, true)
	if err != nil {
		fatalf("convert address %s to bech32: %v", address.Hex(), err)
	}
	result, err := bech32.Encode("one", converted)
	if err != nil {
		fatalf("encode address %s as bech32: %v", address.Hex(), err)
	}
	return result
}

func latestExplorerActivity(
	db *nativeleveldb.DB,
	address common.Address,
	prefixName string,
	activityType string,
	shard uint32,
	cutoff uint64,
	chainDB ethdb.Database,
	bodyCache map[uint64]*hmytypes.Body,
	counts *counters,
) (activity, bool) {
	prefix := append([]byte(prefixName), []byte(oneAddress(address))...)
	iterator := db.NewIterator(util.BytesPrefix(prefix), nil)
	defer iterator.Release()
	expectedLength := len(prefix) + 8 + 8 + common.HashLength
	for valid := iterator.Last(); valid; valid = iterator.Prev() {
		key := iterator.Key()
		if len(key) != expectedLength {
			fatalf(
				"explorer index key for %s has %d bytes, expected %d",
				address.Hex(),
				len(key),
				expectedLength,
			)
		}
		counts.TransactionLookupEntries++
		blockStart := len(prefix)
		number := binary.BigEndian.Uint64(key[blockStart : blockStart+8])
		if number > cutoff {
			continue
		}
		index := binary.BigEndian.Uint64(
			key[blockStart+8 : blockStart+16],
		)
		hash := common.BytesToHash(key[blockStart+16:])
		value := iterator.Value()
		detail := "unknown"
		if len(value) == 1 {
			switch value[0] {
			case 1:
				detail = "sender"
			case 2:
				detail = "recipient"
			}
		}
		result := activity{
			Block:  number,
			Shard:  shard,
			Type:   activityType,
			TxHash: hash.Hex(),
			Index:  index,
			Detail: detail,
		}
		canonicalHash, canonical := verifyCanonicalActivity(
			chainDB,
			result,
			bodyCache,
		)
		if !canonical {
			counts.StaleIndexEntries++
			continue
		}
		result.TxHash = canonicalHash
		if activityType == "regular" {
			counts.RegularTransactions++
		} else {
			counts.StakingTransactions++
		}
		return result, true
	}
	if err := iterator.Error(); err != nil {
		fatalf("iterate explorer index for %s: %v", address.Hex(), err)
	}
	return activity{}, false
}

func laterActivity(left, right activity) activity {
	if left.TxHash == "" {
		return right
	}
	if right.TxHash == "" {
		return left
	}
	leftKey := fmt.Sprintf(
		"%020d/%020d/%s/%s",
		left.Block,
		left.Index,
		left.Type,
		left.TxHash,
	)
	rightKey := fmt.Sprintf(
		"%020d/%020d/%s/%s",
		right.Block,
		right.Index,
		right.Type,
		right.TxHash,
	)
	if rightKey > leftKey {
		return right
	}
	return left
}

func verifyCanonicalActivity(
	db ethdb.Database,
	record activity,
	bodyCache map[uint64]*hmytypes.Body,
) (string, bool) {
	body, exists := bodyCache[record.Block]
	if !exists {
		hash := readCanonicalHash(db, record.Block)
		body = readBody(db, record.Block, hash)
		bodyCache[record.Block] = body
	}
	expectedHash := common.HexToHash(record.TxHash)
	index := int(record.Index)
	if record.Type == "regular" {
		transactions := body.Transactions()
		if index >= len(transactions) {
			return "", false
		}
		transaction := transactions[index]
		if transaction.HashByType() != expectedHash &&
			transaction.ConvertToEth().Hash() != expectedHash {
			return "", false
		}
		return transaction.HashByType().Hex(), true
	}
	transactions := body.StakingTransactions()
	if index >= len(transactions) ||
		transactions[index].Hash() != expectedHash {
		return "", false
	}
	return transactions[index].Hash().Hex(), true
}

func scanExplorerDatabase(
	explorerPath string,
	chainDB ethdb.Database,
	candidates []candidate,
	shard uint32,
	cutoff uint64,
) ([]activity, map[common.Address]int, counters) {
	explorerDB, err := nativeleveldb.OpenFile(
		explorerPath,
		&opt.Options{ReadOnly: true},
	)
	if err != nil {
		fatalf("open explorer-node database: %v", err)
	}
	defer explorerDB.Close()

	records := make([]activity, len(candidates))
	unresolved := make(map[common.Address]int)
	timestamps := make(map[uint64]uint64)
	bodies := make(map[uint64]*hmytypes.Body)
	counts := counters{}
	for index, candidate := range candidates {
		counts.CandidateChecks++
		normal, haveNormal := latestExplorerActivity(
			explorerDB,
			candidate.Address,
			"at",
			"regular",
			shard,
			cutoff,
			chainDB,
			bodies,
			&counts,
		)
		var stakingActivity activity
		haveStaking := false
		if shard == 0 {
			stakingActivity, haveStaking = latestExplorerActivity(
				explorerDB,
				candidate.Address,
				"stk",
				"staking",
				shard,
				cutoff,
				chainDB,
				bodies,
				&counts,
			)
		}
		if !haveNormal && !haveStaking {
			unresolved[candidate.Address] = index
			continue
		}
		selected := laterActivity(normal, stakingActivity)
		timestamp, exists := timestamps[selected.Block]
		if !exists {
			hash := readCanonicalHash(chainDB, selected.Block)
			timestamp = readTimestamp(
				chainDB,
				selected.Block,
				hash,
			)
			timestamps[selected.Block] = timestamp
			counts.CanonicalBlocksRead++
		}
		selected.TimestampUnix = timestamp
		selected.TimeUTC = time.Unix(
			int64(timestamp),
			0,
		).UTC().Format(time.RFC3339)
		records[index] = selected
		if (index+1)%10_000 == 0 {
			fmt.Fprintf(
				os.Stderr,
				"progress shard=%d candidates=%d/%d found=%d\n",
				shard,
				index+1,
				len(candidates),
				index+1-len(unresolved),
			)
		}
	}
	counts.TransactionBlocks = uint64(len(timestamps))
	return records, unresolved, counts
}

func writeCSV(path string, candidates []candidate, records []activity) {
	if dir := filepath.Dir(path); dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			fatalf("create output directory: %v", err)
		}
	}
	partial := path + ".partial"
	file, err := os.OpenFile(
		partial,
		os.O_CREATE|os.O_EXCL|os.O_WRONLY,
		0o600,
	)
	if err != nil {
		fatalf("create output: %v", err)
	}
	buffer := bufio.NewWriterSize(file, 1024*1024)
	writer := csv.NewWriter(buffer)
	if err := writer.Write(outputHeader); err != nil {
		fatalf("write output header: %v", err)
	}
	for index, candidate := range candidates {
		record := records[index]
		row := []string{
			candidate.SecureKey.Hex(),
			candidate.Address.Hex(),
			"",
			"",
			"",
			"",
			"",
			"",
			"",
			"",
		}
		if record.TimeUTC != "" {
			row[2] = record.TimeUTC
			row[3] = strconv.FormatUint(record.TimestampUnix, 10)
			row[4] = strconv.FormatUint(record.Block, 10)
			row[5] = strconv.FormatUint(uint64(record.Shard), 10)
			row[6] = record.Type
			row[7] = record.TxHash
			row[8] = strconv.FormatUint(record.Index, 10)
			row[9] = record.Detail
		}
		if err := writer.Write(row); err != nil {
			fatalf("write output row: %v", err)
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush output CSV: %v", err)
	}
	if err := buffer.Flush(); err != nil {
		fatalf("flush output buffer: %v", err)
	}
	if err := file.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := file.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, path); err != nil {
		fatalf("replace output: %v", err)
	}
}

func writeJSON(path string, value interface{}) {
	if dir := filepath.Dir(path); dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			fatalf("create summary directory: %v", err)
		}
	}
	partial := path + ".partial"
	file, err := os.OpenFile(
		partial,
		os.O_CREATE|os.O_EXCL|os.O_WRONLY,
		0o600,
	)
	if err != nil {
		fatalf("create summary: %v", err)
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		fatalf("encode summary: %v", err)
	}
	if err := file.Sync(); err != nil {
		fatalf("sync summary: %v", err)
	}
	if err := file.Close(); err != nil {
		fatalf("close summary: %v", err)
	}
	if err := os.Rename(partial, path); err != nil {
		fatalf("replace summary: %v", err)
	}
}

func main() {
	var (
		dbPath         = flag.String("db", "", "canonical Harmony LevelDB")
		explorerDBPath = flag.String(
			"explorer-db",
			"",
			"optional explorer-node LevelDB for fast per-address lookup",
		)
		candidatesPath = flag.String(
			"candidates",
			"",
			">=1,000 ONE candidate CSV",
		)
		shard       = flag.Uint("shard", 0, "source shard (0 or 1)")
		cutoffBlock = flag.Uint64(
			"cutoff-block",
			0,
			"last included canonical block",
		)
		cutoffHash = flag.String(
			"cutoff-hash",
			"",
			"expected canonical cutoff hash",
		)
		outputPath  = flag.String("output", "", "activity CSV")
		summaryPath = flag.String("summary", "", "summary JSON")
		cacheMB     = flag.Int("cache-mb", 1024, "LevelDB cache MiB")
		handles     = flag.Int("handles", 2048, "LevelDB handles")
		replace     = flag.Bool("replace", false, "replace outputs")
	)
	flag.Parse()
	if *dbPath == "" ||
		*candidatesPath == "" ||
		*cutoffBlock == 0 ||
		*cutoffHash == "" ||
		*outputPath == "" ||
		*summaryPath == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *shard > 1 {
		fatalf("shard must be 0 or 1")
	}
	cutoffHashValue, err := parseHash(*cutoffHash)
	if err != nil {
		fatalf("invalid cutoff hash: %v", err)
	}
	for _, path := range []string{*outputPath, *summaryPath} {
		if _, statErr := os.Stat(path); statErr == nil && !*replace {
			fatalf("%s exists; pass -replace", path)
		} else if statErr != nil && !os.IsNotExist(statErr) {
			fatalf("stat %s: %v", path, statErr)
		}
		if removeErr := os.Remove(path + ".partial"); removeErr != nil &&
			!os.IsNotExist(removeErr) {
			fatalf("remove stale partial: %v", removeErr)
		}
	}

	started := time.Now()
	candidates, initialAddresses := loadCandidates(
		*candidatesPath,
		uint32(*shard),
		*cutoffBlock,
	)
	disk, err := gethleveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	if readCanonicalHash(db, *cutoffBlock) != cutoffHashValue {
		fatalf("cutoff hash does not match database")
	}
	var (
		records    []activity
		unresolved map[common.Address]int
		counts     counters
		indexTail  *uint64
		sourceKind string
	)
	if *explorerDBPath != "" {
		records, unresolved, counts = scanExplorerDatabase(
			*explorerDBPath,
			db,
			candidates,
			uint32(*shard),
			*cutoffBlock,
		)
		sourceKind = "local explorer-node address index and canonical headers"
	} else {
		unresolved = initialAddresses
		records = make([]activity, len(candidates))
		indexTail = transactionIndexTail(db)
		if indexTail != nil && *indexTail > 0 {
			fatalf(
				"transaction index starts at block %d; full history required",
				*indexTail,
			)
		}
		bitset, lookupEntries, blockCount := transactionBlocks(
			db,
			*cutoffBlock,
		)
		counts = counters{
			TransactionLookupEntries: lookupEntries,
			TransactionBlocks:        blockCount,
		}
		for number := *cutoffBlock; ; number-- {
			if hasBlock(bitset, number) {
				hash := readCanonicalHash(db, number)
				body := readBody(db, number, hash)
				counts.CanonicalBlocksRead++
				blockMatches := make(map[int]activity)
				transactions := body.Transactions()
				for index := len(transactions) - 1; index >= 0; index-- {
					scanRegular(
						transactions[index],
						uint64(index),
						uint32(*shard),
						unresolved,
						blockMatches,
						&counts,
					)
				}
				stakingTransactions := body.StakingTransactions()
				for index := len(stakingTransactions) - 1; index >= 0; index-- {
					scanStaking(
						stakingTransactions[index],
						uint64(index),
						uint32(*shard),
						unresolved,
						blockMatches,
						&counts,
					)
				}
				if len(blockMatches) > 0 {
					timestamp := readTimestamp(db, number, hash)
					for index, record := range blockMatches {
						record.Block = number
						record.TimestampUnix = timestamp
						record.TimeUTC = time.Unix(
							int64(timestamp),
							0,
						).UTC().Format(time.RFC3339)
						records[index] = record
						delete(unresolved, candidates[index].Address)
					}
				}
			}
			if number%5_000_000 == 0 {
				fmt.Fprintf(
					os.Stderr,
					"progress shard=%d block=%d found=%d unresolved=%d tx_blocks=%d\n",
					*shard,
					number,
					len(candidates)-len(unresolved),
					len(unresolved),
					counts.CanonicalBlocksRead,
				)
			}
			if number == 0 || len(unresolved) == 0 {
				break
			}
		}
		sourceKind = "canonical transaction lookup table and block bodies"
	}
	if err := db.Close(); err != nil {
		fatalf("close database: %v", err)
	}

	writeCSV(*outputPath, candidates, records)
	result := summary{
		Status:               "passed",
		SourceKind:           sourceKind,
		DBPath:               *dbPath,
		ExplorerDBPath:       *explorerDBPath,
		CandidatesPath:       *candidatesPath,
		CandidatesSHA256:     fileSHA256(*candidatesPath),
		Candidates:           uint64(len(candidates)),
		Shard:                uint32(*shard),
		CutoffBlock:          *cutoffBlock,
		CutoffHash:           cutoffHashValue.Hex(),
		TransactionIndexTail: indexTail,
		ActivityFound:        uint64(len(candidates) - len(unresolved)),
		ActivityNotFound:     uint64(len(unresolved)),
		Counters:             counts,
		OutputPath:           *outputPath,
		OutputSHA256:         fileSHA256(*outputPath),
		ElapsedMilliseconds:  time.Since(started).Milliseconds(),
	}
	writeJSON(*summaryPath, result)
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("print summary: %v", err)
	}
}
