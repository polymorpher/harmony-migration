// Command account-directional-activity reports, per address and shard, the
// newest transaction signed by the address and the newest direct inbound
// regular transaction from another sender at or before a cutoff block.
//
// It reads the explorer-node per-address index newest first and classifies
// every examined entry from the canonical block body: the index's sent or
// received flag is not trusted because an entry whose sender and index
// address coincide (a self-transfer, or a validator's own staking message) is
// written twice under the same key and keeps only the received flag.
package main

import (
	"bufio"
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
	"sync"
	"sync/atomic"
	"time"

	"github.com/btcsuite/btcutil/bech32"
	"github.com/ethereum/go-ethereum/common"
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

var outputHeader = []string{
	"address",
	"shard",
	"cutoff_nonce",
	"signed_check",
	"last_signed_time_utc",
	"last_signed_timestamp_unix",
	"last_signed_block",
	"last_signed_type",
	"last_signed_nonce",
	"last_signed_tx_hash",
	"last_inbound_time_utc",
	"last_inbound_timestamp_unix",
	"last_inbound_block",
	"last_inbound_from",
	"last_inbound_value_atto",
	"last_inbound_tx_hash",
	"index_entries_examined",
	"stale_index_entries",
}

type candidate struct {
	Address common.Address
	// Nonce is the cutoff nonce on this shard, or -1 when unknown.
	Nonce int64
	// Contract is 1 for EVM code (not a validator wrapper), 0 for none, -1 when unknown.
	Contract int
}

type event struct {
	Found     bool
	Block     uint64
	Index     uint64
	Timestamp uint64
	Type      string
	Nonce     uint64
	TxHash    string
	From      common.Address
	Value     *big.Int
}

type result struct {
	Signed   event
	Inbound  event
	Check    string
	Examined uint64
	Stale    uint64
}

type counters struct {
	Candidates       uint64            `json:"candidates"`
	EntriesExamined  uint64            `json:"index_entries_examined"`
	StaleEntries     uint64            `json:"stale_index_entries"`
	BodiesRead       uint64            `json:"canonical_bodies_read"`
	HeadersRead      uint64            `json:"canonical_headers_read"`
	SignedFound      uint64            `json:"signed_found"`
	InboundFound     uint64            `json:"inbound_found"`
	SignedChecks     map[string]uint64 `json:"signed_checks"`
	EntryLimitHits   uint64            `json:"entry_limit_hits"`
	SignedFromStake  uint64            `json:"signed_from_staking"`
	SelfTransfersRec uint64            `json:"signed_found_under_received_flag"`
	PositionRepairs  uint64            `json:"index_position_repairs"`
	BlockRepairs     uint64            `json:"index_block_repairs"`
}

type summary struct {
	Status              string   `json:"status"`
	SourceKind          string   `json:"source_kind"`
	DBPath              string   `json:"db_path"`
	ExplorerDBPath      string   `json:"explorer_db_path"`
	CandidatesPath      string   `json:"candidates_path"`
	CandidatesSHA256    string   `json:"candidates_sha256"`
	Shard               uint32   `json:"shard"`
	CutoffBlock         uint64   `json:"cutoff_block"`
	CutoffHash          string   `json:"cutoff_hash"`
	MaxEntries          uint64   `json:"max_entries_per_index"`
	Workers             int      `json:"workers"`
	Counters            counters `json:"counters"`
	OutputPath          string   `json:"output_path"`
	OutputSHA256        string   `json:"output_sha256"`
	ElapsedMilliseconds int64    `json:"elapsed_milliseconds"`
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
	return append(append([]byte{'h'}, encodeBlockNumber(number)...), 'n')
}

func bodyKey(number uint64, hash common.Hash) []byte {
	return append(append([]byte{'b'}, encodeBlockNumber(number)...), hash.Bytes()...)
}

func headerKey(number uint64, hash common.Hash) []byte {
	return append(append([]byte{'h'}, encodeBlockNumber(number)...), hash.Bytes()...)
}

func readRequired(db ethdb.Database, key []byte, label string) []byte {
	value, err := db.Get(key)
	if err != nil {
		fatalf("read %s: %v", label, err)
	}
	return value
}

func readCanonicalHash(db ethdb.Database, number uint64) common.Hash {
	value := readRequired(db, canonicalHashKey(number), fmt.Sprintf("canonical hash at block %d", number))
	if len(value) != common.HashLength {
		fatalf("canonical hash at block %d has wrong length", number)
	}
	return common.BytesToHash(value)
}

func readTimestamp(db ethdb.Database, number uint64, hash common.Hash) uint64 {
	value := readRequired(db, headerKey(number, hash), fmt.Sprintf("canonical header at block %d", number))
	header := new(block.Header)
	if err := rlp.DecodeBytes(value, header); err != nil {
		fatalf("decode canonical header at block %d: %v", number, err)
	}
	if header.Hash() != hash || header.Number().Uint64() != number {
		fatalf("canonical header identity mismatch at block %d", number)
	}
	return header.Time().Uint64()
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

func oneAddress(address common.Address) string {
	converted, err := bech32.ConvertBits(address.Bytes(), 8, 5, true)
	if err != nil {
		fatalf("convert address %s to bech32: %v", address.Hex(), err)
	}
	encoded, err := bech32.Encode("one", converted)
	if err != nil {
		fatalf("encode address %s as bech32: %v", address.Hex(), err)
	}
	return encoded
}

func parseHash(value string) (common.Hash, error) {
	decoded, err := hex.DecodeString(strings.TrimPrefix(value, "0x"))
	if err != nil || len(decoded) != common.HashLength {
		return common.Hash{}, fmt.Errorf("invalid 32-byte hash %q", value)
	}
	return common.BytesToHash(decoded), nil
}

func loadCandidates(path string, shard uint32) []candidate {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open candidates: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1<<20))
	header, err := reader.Read()
	if err != nil {
		fatalf("read candidate header: %v", err)
	}
	indexes := make(map[string]int, len(header))
	for position, name := range header {
		indexes[name] = position
	}
	nonceField := fmt.Sprintf("nonce_shard%d", shard)
	for _, name := range []string{"address", nonceField, "is_contract"} {
		if _, exists := indexes[name]; !exists {
			fatalf("candidate CSV is missing %s", name)
		}
	}
	seen := make(map[common.Address]bool)
	var result []candidate
	for line := 2; ; line++ {
		row, readErr := reader.Read()
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			fatalf("read candidate row %d: %v", line, readErr)
		}
		text := row[indexes["address"]]
		if !common.IsHexAddress(text) {
			fatalf("candidate row %d has invalid address %q", line, text)
		}
		address := common.HexToAddress(text)
		if seen[address] {
			fatalf("candidate row %d duplicates %s", line, text)
		}
		seen[address] = true
		nonce, err := strconv.ParseInt(row[indexes[nonceField]], 10, 64)
		if err != nil || nonce < -1 {
			fatalf("candidate row %d has invalid %s", line, nonceField)
		}
		contract, err := strconv.Atoi(row[indexes["is_contract"]])
		if err != nil || contract < -1 || contract > 1 {
			fatalf("candidate row %d has invalid is_contract", line)
		}
		result = append(result, candidate{Address: address, Nonce: nonce, Contract: contract})
	}
	return result
}

type worker struct {
	chain      ethdb.Database
	explorer   *nativeleveldb.DB
	shard      uint32
	cutoff     uint64
	maxEntries uint64
	blocks     map[uint64]*canonicalBlock
	counts     counters
}

// canonicalBlock holds one decoded canonical body. Body.Transactions() and
// Body.StakingTransactions() deep-copy the whole list on every call, so they
// are called once per block.
type canonicalBlock struct {
	hash      common.Hash
	regular   []*hmytypes.Transaction
	staking   []*staking.StakingTransaction
	positions map[common.Hash]int
	stakingAt map[common.Hash]int
}

func (b *canonicalBlock) position(hash common.Hash, isStaking bool) (int, bool) {
	if isStaking {
		if b.stakingAt == nil {
			b.stakingAt = make(map[common.Hash]int, len(b.staking))
			for position, tx := range b.staking {
				b.stakingAt[tx.Hash()] = position
			}
		}
		position, ok := b.stakingAt[hash]
		return position, ok
	}
	if b.positions == nil {
		b.positions = make(map[common.Hash]int, 2*len(b.regular))
		for position, tx := range b.regular {
			b.positions[tx.HashByType()] = position
			b.positions[tx.ConvertToEth().Hash()] = position
			b.positions[tx.Hash()] = position
		}
	}
	position, ok := b.positions[hash]
	return position, ok
}

func (w *worker) canonicalBlock(number uint64) *canonicalBlock {
	if cached, ok := w.blocks[number]; ok {
		return cached
	}
	if len(w.blocks) >= 8192 {
		w.blocks = make(map[uint64]*canonicalBlock)
	}
	hash := readCanonicalHash(w.chain, number)
	value := readRequired(w.chain, bodyKey(number, hash), fmt.Sprintf("canonical body at block %d", number))
	body := new(hmytypes.Body)
	if err := rlp.DecodeBytes(value, body); err != nil {
		fatalf("decode canonical body at block %d: %v", number, err)
	}
	w.counts.BodiesRead++
	cached := &canonicalBlock{hash: hash, regular: body.Transactions(), staking: body.StakingTransactions()}
	w.blocks[number] = cached
	return cached
}

// lookup reads the chain's transaction lookup entry, which records the block
// (and, in the current format, the position) a transaction hash was last
// written to.
func (w *worker) lookup(hash common.Hash) (uint64, uint64, bool, bool) {
	value, err := w.chain.Get(append([]byte{'l'}, hash.Bytes()...))
	if err != nil || len(value) == 0 {
		return 0, 0, false, false
	}
	var entry rawdb.TxLookupEntry
	if err := rlp.DecodeBytes(value, &entry); err == nil && entry.BlockHash != (common.Hash{}) {
		return entry.BlockIndex, entry.Index, true, true
	}
	if len(value) > 8 {
		return 0, 0, false, false
	}
	return new(big.Int).SetBytes(value).Uint64(), 0, false, true
}

// locate returns the canonical block and position of the transaction named by
// an index entry: the recorded position in the given block, else anywhere in
// that block. A transaction absent from the canonical body is stale.
func (w *worker) locate(number, index uint64, hash common.Hash, isStaking bool) (*canonicalBlock, uint64, bool) {
	cached := w.canonicalBlock(number)
	if isStaking {
		if index < uint64(len(cached.staking)) && cached.staking[index].Hash() == hash {
			return cached, index, true
		}
	} else if index < uint64(len(cached.regular)) {
		tx := cached.regular[index]
		if tx.HashByType() == hash || tx.ConvertToEth().Hash() == hash {
			return cached, index, true
		}
	}
	if position, ok := cached.position(hash, isStaking); ok {
		return cached, uint64(position), true
	}
	return nil, 0, false
}

func later(block, index uint64, current event) bool {
	return !current.Found || block > current.Block || (block == current.Block && index > current.Index)
}

func (w *worker) stamp(value *event) {
	value.Timestamp = readTimestamp(w.chain, value.Block, w.canonicalBlock(value.Block).hash)
	w.counts.HeadersRead++
}

// scan reads every entry of one per-address index. The key's block number is
// not trusted for ordering (some keys record an earlier or discarded block),
// so each entry's block comes from the chain's lookup table, and only entries
// that could be newer than the current best are decoded. Staking entries never
// count as inbound.
func (w *worker) scan(prefixName string, address common.Address, wantSigned, wantInbound bool, out *result) (event, event) {
	var signed, inbound event
	prefix := append([]byte(prefixName), []byte(oneAddress(address))...)
	iterator := w.explorer.NewIterator(util.BytesPrefix(prefix), nil)
	defer iterator.Release()
	expected := len(prefix) + 8 + 8 + common.HashLength
	isStaking := prefixName == "stk"
	var examined uint64
	for valid := iterator.Last(); valid && (wantSigned || wantInbound); valid = iterator.Prev() {
		key := iterator.Key()
		if len(key) != expected {
			fatalf("explorer index key for %s has %d bytes, expected %d", address.Hex(), len(key), expected)
		}
		examined++
		if examined > w.maxEntries {
			w.counts.EntryLimitHits++
			break
		}
		keyBlock := binary.BigEndian.Uint64(key[len(prefix) : len(prefix)+8])
		keyIndex := binary.BigEndian.Uint64(key[len(prefix)+8 : len(prefix)+16])
		expectedHash := common.BytesToHash(key[len(prefix)+16:])
		flag := byte(0)
		if value := iterator.Value(); len(value) == 1 {
			flag = value[0]
		}
		if flag == 1 && !wantSigned {
			continue
		}
		number, index := keyBlock, keyIndex
		if block, position, positioned, ok := w.lookup(expectedHash); ok {
			if block != keyBlock {
				w.counts.BlockRepairs++
			}
			number = block
			if positioned {
				index = position
			}
		}
		if number > w.cutoff {
			continue
		}
		// a sent-flag entry is never inbound (a self-transfer keeps only the received flag)
		couldSign := wantSigned && later(number, index, signed)
		couldReceive := wantInbound && flag != 1 && !isStaking && later(number, index, inbound)
		if !couldSign && !couldReceive {
			continue
		}
		cached, index, ok := w.locate(number, index, expectedHash, isStaking)
		if !ok {
			out.Stale++
			continue
		}
		if index != keyIndex || number != keyBlock {
			w.counts.PositionRepairs++
		}
		var (
			sender common.Address
			nonce  uint64
			txHash common.Hash
			to     *common.Address
			amount *big.Int
			err    error
		)
		if isStaking {
			tx := cached.staking[index]
			sender, err = tx.SenderAddress()
			nonce, txHash = tx.Nonce(), tx.Hash()
		} else {
			tx := cached.regular[index]
			sender, err = tx.SenderAddress()
			nonce, txHash, to, amount = tx.Nonce(), tx.HashByType(), tx.To(), tx.Value()
		}
		if err != nil {
			fatalf("recover sender for %s: %v", txHash.Hex(), err)
		}
		kind := "regular"
		if isStaking {
			kind = "staking"
		}
		current := event{Found: true, Block: number, Index: index, Type: kind, Nonce: nonce, TxHash: txHash.Hex(), From: sender, Value: amount}
		if sender == address {
			if couldSign {
				signed = current
				if flag == 2 {
					w.counts.SelfTransfersRec++
				}
			}
		} else if couldReceive && to != nil && *to == address {
			inbound = current
		}
	}
	if err := iterator.Error(); err != nil {
		fatalf("iterate explorer index for %s: %v", address.Hex(), err)
	}
	out.Examined += examined
	return signed, inbound
}

func (w *worker) process(value candidate) result {
	var out result
	wantSigned := value.Contract != 1 && value.Nonce != 0
	regularSigned, inbound := w.scan("at", value.Address, wantSigned, true, &out)
	out.Inbound = inbound
	signed := regularSigned
	if wantSigned && w.shard == 0 {
		stakingSigned, _ := w.scan("stk", value.Address, true, false, &out)
		if stakingSigned.Found && (!signed.Found || stakingSigned.Nonce > signed.Nonce) {
			signed = stakingSigned
			w.counts.SignedFromStake++
		}
	}
	out.Signed = signed
	switch {
	case value.Contract == 1:
		out.Check = "contract"
	case value.Nonce == 0:
		out.Check = "never_signed"
	case !signed.Found && value.Nonce < 0:
		out.Check = "none_found_nonce_unknown"
	case !signed.Found:
		out.Check = "index_gap"
	case value.Nonce < 0:
		out.Check = "nonce_unknown"
	case int64(signed.Nonce) == value.Nonce-1:
		out.Check = "verified"
	case int64(signed.Nonce) < value.Nonce-1:
		out.Check = "index_gap"
	default:
		fatalf("%s signed nonce %d exceeds cutoff nonce %d", value.Address.Hex(), signed.Nonce, value.Nonce)
	}
	if out.Signed.Found {
		w.stamp(&out.Signed)
		w.counts.SignedFound++
	}
	if out.Inbound.Found {
		w.stamp(&out.Inbound)
		w.counts.InboundFound++
	}
	w.counts.SignedChecks[out.Check]++
	w.counts.EntriesExamined += out.Examined
	w.counts.StaleEntries += out.Stale
	return out
}

func formatTime(timestamp uint64) string {
	return time.Unix(int64(timestamp), 0).UTC().Format(time.RFC3339)
}

func writeCSV(path string, shard uint32, candidates []candidate, results []result) {
	if dir := filepath.Dir(path); dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			fatalf("create output directory: %v", err)
		}
	}
	partial := path + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	buffer := bufio.NewWriterSize(file, 1<<20)
	writer := csv.NewWriter(buffer)
	if err := writer.Write(outputHeader); err != nil {
		fatalf("write output header: %v", err)
	}
	for position, value := range candidates {
		item := results[position]
		row := make([]string, len(outputHeader))
		row[0] = strings.ToLower(value.Address.Hex())
		row[1] = strconv.FormatUint(uint64(shard), 10)
		if value.Nonce >= 0 {
			row[2] = strconv.FormatInt(value.Nonce, 10)
		}
		row[3] = item.Check
		if s := item.Signed; s.Found {
			row[4], row[5], row[6] = formatTime(s.Timestamp), strconv.FormatUint(s.Timestamp, 10), strconv.FormatUint(s.Block, 10)
			row[7], row[8], row[9] = s.Type, strconv.FormatUint(s.Nonce, 10), s.TxHash
		}
		if in := item.Inbound; in.Found {
			row[10], row[11], row[12] = formatTime(in.Timestamp), strconv.FormatUint(in.Timestamp, 10), strconv.FormatUint(in.Block, 10)
			row[13], row[14], row[15] = strings.ToLower(in.From.Hex()), in.Value.String(), in.TxHash
		}
		row[16] = strconv.FormatUint(item.Examined, 10)
		row[17] = strconv.FormatUint(item.Stale, 10)
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
	partial := path + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create summary: %v", err)
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		fatalf("encode summary: %v", err)
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
		dbPath         = flag.String("db", "", "canonical Harmony LevelDB for the shard")
		explorerDBPath = flag.String("explorer-db", "", "explorer-node LevelDB for the shard")
		candidatesPath = flag.String("candidates", "", "CSV with address, nonce_shard0, nonce_shard1, is_contract (-1 = unknown)")
		shard          = flag.Uint("shard", 0, "shard (0 or 1)")
		cutoffBlock    = flag.Uint64("cutoff-block", 0, "last included canonical block")
		cutoffHash     = flag.String("cutoff-hash", "", "expected canonical cutoff hash")
		outputPath     = flag.String("output", "", "output CSV")
		summaryPath    = flag.String("summary", "", "summary JSON")
		workers        = flag.Int("workers", 16, "parallel readers")
		maxEntries     = flag.Uint64("max-entries", 5_000_000, "index entries examined per address and index before giving up")
		cacheMB        = flag.Int("cache-mb", 2048, "LevelDB cache MiB")
		handles        = flag.Int("handles", 4096, "LevelDB handles")
		replace        = flag.Bool("replace", false, "replace outputs")
	)
	flag.Parse()
	if *dbPath == "" || *explorerDBPath == "" || *candidatesPath == "" || *cutoffBlock == 0 ||
		*cutoffHash == "" || *outputPath == "" || *summaryPath == "" || *workers < 1 {
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
		}
		_ = os.Remove(path + ".partial")
	}

	started := time.Now()
	candidates := loadCandidates(*candidatesPath, uint32(*shard))
	disk, err := gethleveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database: %v", err)
	}
	chain := rawdb.NewDatabase(disk)
	if readCanonicalHash(chain, *cutoffBlock) != cutoffHashValue {
		fatalf("cutoff hash does not match database")
	}
	explorer, err := nativeleveldb.OpenFile(*explorerDBPath, &opt.Options{ReadOnly: true})
	if err != nil {
		fatalf("open explorer-node database: %v", err)
	}

	results := make([]result, len(candidates))
	jobs := make(chan int, 4096)
	var done uint64
	var group sync.WaitGroup
	pool := make([]*worker, *workers)
	for i := range pool {
		pool[i] = &worker{
			chain: chain, explorer: explorer, shard: uint32(*shard), cutoff: *cutoffBlock, maxEntries: *maxEntries,
			blocks: make(map[uint64]*canonicalBlock),
			counts: counters{SignedChecks: make(map[string]uint64)},
		}
		group.Add(1)
		go func(w *worker) {
			defer group.Done()
			for position := range jobs {
				results[position] = w.process(candidates[position])
				if n := atomic.AddUint64(&done, 1); n%50_000 == 0 {
					fmt.Fprintf(os.Stderr, "progress shard=%d %d/%d elapsed=%s\n", *shard, n, len(candidates), time.Since(started).Round(time.Second))
				}
			}
		}(pool[i])
	}
	for position := range candidates {
		jobs <- position
	}
	close(jobs)
	group.Wait()
	explorer.Close()
	if err := chain.Close(); err != nil {
		fatalf("close database: %v", err)
	}

	total := counters{Candidates: uint64(len(candidates)), SignedChecks: make(map[string]uint64)}
	for _, w := range pool {
		c := w.counts
		total.EntriesExamined += c.EntriesExamined
		total.StaleEntries += c.StaleEntries
		total.BodiesRead += c.BodiesRead
		total.HeadersRead += c.HeadersRead
		total.SignedFound += c.SignedFound
		total.InboundFound += c.InboundFound
		total.EntryLimitHits += c.EntryLimitHits
		total.SignedFromStake += c.SignedFromStake
		total.SelfTransfersRec += c.SelfTransfersRec
		total.PositionRepairs += c.PositionRepairs
		total.BlockRepairs += c.BlockRepairs
		for key, value := range c.SignedChecks {
			total.SignedChecks[key] += value
		}
	}
	writeCSV(*outputPath, uint32(*shard), candidates, results)
	report := summary{
		Status:              "passed",
		SourceKind:          "explorer-node per-address index; every examined entry classified from the canonical block body",
		DBPath:              *dbPath,
		ExplorerDBPath:      *explorerDBPath,
		CandidatesPath:      *candidatesPath,
		CandidatesSHA256:    fileSHA256(*candidatesPath),
		Shard:               uint32(*shard),
		CutoffBlock:         *cutoffBlock,
		CutoffHash:          cutoffHashValue.Hex(),
		MaxEntries:          *maxEntries,
		Workers:             *workers,
		Counters:            total,
		OutputPath:          *outputPath,
		OutputSHA256:        fileSHA256(*outputPath),
		ElapsedMilliseconds: time.Since(started).Milliseconds(),
	}
	writeJSON(*summaryPath, report)
	if err := json.NewEncoder(os.Stdout).Encode(report); err != nil {
		fatalf("print summary: %v", err)
	}
}
