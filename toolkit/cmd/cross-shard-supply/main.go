package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/polymorpher/harmony-migration/toolkit/internal/historyguard"
)

var (
	receiptPrefix  = []byte("cxReceipt")
	cxLookupPrefix = []byte("cx")
)

type txLookupEntry struct {
	BlockHash  common.Hash
	BlockIndex uint64
	Index      uint64
}

type cxReceipt struct {
	TxHash    common.Hash
	From      common.Address
	To        *common.Address
	ShardID   uint32
	ToShardID uint32
	Amount    *big.Int
}

type directionTotals struct {
	SourceShard                uint32                `json:"source_shard"`
	EmptyReceiptGroups         uint64                `json:"empty_receipt_groups"`
	CanonicalReceiptGroups     uint64                `json:"canonical_receipt_groups"`
	NonCanonicalReceiptGroups  uint64                `json:"noncanonical_receipt_groups"`
	MissingCanonicalHashGroups uint64                `json:"missing_canonical_hash_groups"`
	AfterCutoffGroups          uint64                `json:"after_cutoff_groups"`
	SpentReceiptGroups         uint64                `json:"spent_receipt_groups"`
	SpentReceiptCount          uint64                `json:"spent_receipt_count"`
	SpentAmountAtto            string                `json:"spent_amount_atto"`
	PendingReceiptGroups       uint64                `json:"pending_receipt_groups"`
	PendingReceiptCount        uint64                `json:"pending_receipt_count"`
	PendingAmountAtto          string                `json:"pending_amount_atto"`
	UnsupportedReceiptGroups   uint64                `json:"unsupported_receipt_groups"`
	UnsupportedReceiptCount    uint64                `json:"unsupported_receipt_count"`
	UnsupportedAmountAtto      string                `json:"unsupported_amount_atto"`
	SourceCoverage             historyguard.Coverage `json:"source_coverage"`
	PendingGroups              []receiptGroup        `json:"pending_groups"`
	UnsupportedGroups          []receiptGroup        `json:"unsupported_groups"`
}

type receiptGroup struct {
	DestinationShard  uint32          `json:"destination_shard"`
	BlockNumber       uint64          `json:"block_number"`
	BlockHash         common.Hash     `json:"block_hash"`
	ReceiptCount      uint64          `json:"receipt_count"`
	AmountAtto        string          `json:"amount_atto"`
	TransactionHashes []common.Hash   `json:"transaction_hashes"`
	Receipts          []receiptDetail `json:"receipts"`
}

type receiptDetail struct {
	TransactionHash common.Hash    `json:"transaction_hash"`
	To              common.Address `json:"to"`
	SecureKey       common.Hash    `json:"secure_key"`
	AmountAtto      string         `json:"amount_atto"`
}

type mutableDirectionTotals struct {
	directionTotals
	spentAmount       *big.Int
	pendingAmount     *big.Int
	unsupportedAmount *big.Int
}

type databaseHistory struct {
	Kind         string              `json:"kind"`
	Report       historyguard.Report `json:"history"`
	LookupCutoff uint64              `json:"lookup_snapshot_cutoff,omitempty"`
}

type summary struct {
	Shard0DB                 string                     `json:"shard0_db"`
	Shard1DB                 string                     `json:"shard1_db"`
	Shard0Cutoff             uint64                     `json:"shard0_cutoff"`
	Shard1Cutoff             uint64                     `json:"shard1_cutoff"`
	SourceShards             []uint32                   `json:"source_shards"`
	CoverageFrom             uint64                     `json:"coverage_from_block"`
	Databases                map[string]databaseHistory `json:"databases"`
	CoverageComplete         bool                       `json:"coverage_complete"`
	IncompleteHistoryAllowed bool                       `json:"incomplete_history_allowed"`
	CoverageProblems         []string                   `json:"coverage_problems"`
	Directions               []directionTotals          `json:"directions"`
	PendingActiveAtto        string                     `json:"pending_active_atto"`
	UnsupportedPendingAtto   string                     `json:"unsupported_pending_atto"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func openDatabase(path string, cache, handles int) ethdb.Database {
	disk, err := leveldb.New(path, cache, handles, "", true)
	if err != nil {
		fatalf("open database %s read-only: %v", path, err)
	}
	return rawdb.NewDatabase(disk)
}

func receiptKeyParts(key []byte) (destination uint32, number uint64, hash common.Hash, ok bool) {
	const suffixLength = 4 + 8 + common.HashLength
	if len(key) != len(receiptPrefix)+suffixLength || !bytes.HasPrefix(key, receiptPrefix) {
		return 0, 0, common.Hash{}, false
	}
	offset := len(receiptPrefix)
	destination = binary.BigEndian.Uint32(key[offset : offset+4])
	number = binary.BigEndian.Uint64(key[offset+4 : offset+12])
	hash = common.BytesToHash(key[offset+12:])
	return destination, number, hash, true
}

func cxLookupKey(hash common.Hash) []byte {
	key := make([]byte, len(cxLookupPrefix)+common.HashLength)
	copy(key, cxLookupPrefix)
	copy(key[len(cxLookupPrefix):], hash.Bytes())
	return key
}

func canonicalHashKey(number uint64) []byte {
	key := make([]byte, 0, 10)
	key = append(key, 'h')
	encoded := make([]byte, 8)
	binary.BigEndian.PutUint64(encoded, number)
	key = append(key, encoded...)
	return append(key, 'n')
}

func readCanonicalHash(
	database ethdb.KeyValueReader,
	number uint64,
) (common.Hash, bool, error) {
	key := canonicalHashKey(number)
	exists, err := database.Has(key)
	if err != nil {
		return common.Hash{}, false, fmt.Errorf(
			"check canonical hash presence: %w",
			err,
		)
	}
	if !exists {
		return common.Hash{}, false, nil
	}
	encoded, err := database.Get(key)
	if err != nil {
		return common.Hash{}, false, fmt.Errorf(
			"read canonical hash: %w",
			err,
		)
	}
	if len(encoded) != common.HashLength {
		return common.Hash{}, false, fmt.Errorf(
			"canonical hash has %d bytes",
			len(encoded),
		)
	}
	return common.BytesToHash(encoded), true, nil
}

func readCXLookup(
	database ethdb.KeyValueReader,
	hash common.Hash,
) ([]byte, bool, error) {
	lookupKey := cxLookupKey(hash)
	applied, err := database.Has(lookupKey)
	if err != nil {
		return nil, false, fmt.Errorf("check lookup presence: %w", err)
	}
	if !applied {
		return nil, false, nil
	}
	encoded, err := database.Get(lookupKey)
	if err != nil {
		return nil, false, fmt.Errorf("read lookup: %w", err)
	}
	if len(encoded) == 0 {
		return nil, false, fmt.Errorf("lookup is present but empty")
	}
	return encoded, true, nil
}

func sumReceipts(
	sourceShard uint32,
	source ethdb.Database,
	destinations map[uint32]ethdb.Database,
	sourceCutoff uint64,
	destinationCutoffs map[uint32]uint64,
	coverageFrom uint64,
) directionTotals {
	result := &mutableDirectionTotals{
		directionTotals:   directionTotals{SourceShard: sourceShard},
		spentAmount:       new(big.Int),
		pendingAmount:     new(big.Int),
		unsupportedAmount: new(big.Int),
	}
	// shards 0 and 1 exist in every era, so every source block from coverageFrom
	// on has a (possibly empty) group toward the other one
	coverage := historyguard.NewCoverageTracker(1-sourceShard, coverageFrom, sourceCutoff)
	iterator := source.NewIterator(receiptPrefix, nil)
	defer iterator.Release()
	started := time.Now()
	lastProgress := started
	var keys uint64

	for iterator.Next() {
		keys++
		if time.Since(lastProgress) >= 10*time.Second {
			fmt.Fprintf(
				os.Stderr,
				"progress source_shard=%d keys=%d canonical=%d spent=%d pending=%d elapsed=%s\n",
				sourceShard,
				keys,
				result.CanonicalReceiptGroups,
				result.SpentReceiptGroups,
				result.PendingReceiptGroups,
				time.Since(started).Round(time.Second),
			)
			lastProgress = time.Now()
		}
		destination, blockNumber, blockHash, ok := receiptKeyParts(iterator.Key())
		if !ok {
			continue
		}
		if err := coverage.Observe(destination, blockNumber); err != nil {
			fatalf("source shard %d receipt coverage: %v", sourceShard, err)
		}
		if bytes.Equal(iterator.Value(), []byte{0xc0}) {
			result.EmptyReceiptGroups++
			continue
		}
		if blockNumber > sourceCutoff {
			result.AfterCutoffGroups++
			continue
		}
		canonicalHash, canonical, err := readCanonicalHash(
			source,
			blockNumber,
		)
		if err != nil {
			fatalf(
				"read source shard %d canonical hash at block %d: %v",
				sourceShard,
				blockNumber,
				err,
			)
		}
		if !canonical {
			result.MissingCanonicalHashGroups++
			continue
		}
		if canonicalHash != blockHash {
			result.NonCanonicalReceiptGroups++
			continue
		}

		var receipts []*cxReceipt
		if err := rlp.DecodeBytes(iterator.Value(), &receipts); err != nil {
			fatalf(
				"decode source shard %d block %d receipt group: %v",
				sourceShard,
				blockNumber,
				err,
			)
		}
		groupAmount := new(big.Int)
		for i, receipt := range receipts {
			if receipt == nil || receipt.To == nil || receipt.Amount == nil {
				fatalf(
					"invalid source shard %d block %d receipt %d",
					sourceShard,
					blockNumber,
					i,
				)
			}
			if receipt.ShardID != sourceShard || receipt.ToShardID != destination {
				fatalf(
					"receipt identity mismatch at source shard %d block %d",
					sourceShard,
					blockNumber,
				)
			}
			if receipt.Amount.Sign() < 0 {
				fatalf(
					"negative receipt amount at source shard %d block %d",
					sourceShard,
					blockNumber,
				)
			}
			groupAmount.Add(groupAmount, receipt.Amount)
		}

		destinationDB, supported := destinations[destination]
		if !supported {
			result.UnsupportedReceiptGroups++
			result.UnsupportedReceiptCount += uint64(len(receipts))
			result.unsupportedAmount.Add(result.unsupportedAmount, groupAmount)
			result.UnsupportedGroups = append(
				result.UnsupportedGroups,
				makeReceiptGroup(destination, blockNumber, blockHash, receipts, groupAmount),
			)
			continue
		}
		result.CanonicalReceiptGroups++
		spent := true
		var destinationBlock uint64
		for _, receipt := range receipts {
			encoded, applied, err := readCXLookup(
				destinationDB,
				receipt.TxHash,
			)
			if err != nil {
				fatalf("read CX lookup %s: %v", receipt.TxHash.Hex(), err)
			}
			if !applied {
				spent = false
				break
			}
			var entry txLookupEntry
			if err := rlp.DecodeBytes(encoded, &entry); err != nil {
				fatalf("decode CX lookup %s: %v", receipt.TxHash.Hex(), err)
			}
			cutoff, ok := destinationCutoffs[destination]
			if !ok || entry.BlockIndex > cutoff {
				spent = false
				break
			}
			canonicalHash, canonical, err := readCanonicalHash(
				destinationDB,
				entry.BlockIndex,
			)
			if err != nil {
				fatalf(
					"read destination shard %d canonical hash at block %d: %v",
					destination,
					entry.BlockIndex,
					err,
				)
			}
			if !canonical || canonicalHash != entry.BlockHash {
				spent = false
				break
			}
			if destinationBlock != 0 && destinationBlock != entry.BlockIndex {
				fatalf("receipt group source shard %d block %d was split across destination blocks", sourceShard, blockNumber)
			}
			destinationBlock = entry.BlockIndex
		}
		if spent {
			result.SpentReceiptGroups++
			result.SpentReceiptCount += uint64(len(receipts))
			result.spentAmount.Add(result.spentAmount, groupAmount)
		} else {
			result.PendingReceiptGroups++
			result.PendingReceiptCount += uint64(len(receipts))
			result.pendingAmount.Add(result.pendingAmount, groupAmount)
			result.PendingGroups = append(
				result.PendingGroups,
				makeReceiptGroup(destination, blockNumber, blockHash, receipts, groupAmount),
			)
		}
	}
	if err := iterator.Error(); err != nil {
		fatalf("iterate source shard %d receipts: %v", sourceShard, err)
	}
	result.SourceCoverage = coverage.Finish()
	result.SpentAmountAtto = result.spentAmount.String()
	result.PendingAmountAtto = result.pendingAmount.String()
	result.UnsupportedAmountAtto = result.unsupportedAmount.String()
	return result.directionTotals
}

func makeReceiptGroup(
	destination uint32,
	blockNumber uint64,
	blockHash common.Hash,
	receipts []*cxReceipt,
	amount *big.Int,
) receiptGroup {
	hashes := make([]common.Hash, len(receipts))
	details := make([]receiptDetail, len(receipts))
	for i, receipt := range receipts {
		hashes[i] = receipt.TxHash
		details[i] = receiptDetail{
			TransactionHash: receipt.TxHash,
			To:              *receipt.To,
			SecureKey:       crypto.Keccak256Hash(receipt.To.Bytes()),
			AmountAtto:      receipt.Amount.String(),
		}
	}
	return receiptGroup{
		DestinationShard:  destination,
		BlockNumber:       blockNumber,
		BlockHash:         blockHash,
		ReceiptCount:      uint64(len(receipts)),
		AmountAtto:        amount.String(),
		TransactionHashes: hashes,
		Receipts:          details,
	}
}

func parseSourceShards(value string) ([]uint32, error) {
	var shards []uint32
	seen := make(map[uint32]bool)
	for _, part := range strings.Split(value, ",") {
		shard, err := strconv.ParseUint(strings.TrimSpace(part), 10, 32)
		if err != nil || shard > 1 {
			return nil, fmt.Errorf("source shard %q is not 0 or 1", part)
		}
		if !seen[uint32(shard)] {
			seen[uint32(shard)] = true
			shards = append(shards, uint32(shard))
		}
	}
	return shards, nil
}

func inspectDatabase(name string, db ethdb.Database, cutoff uint64, probes int) (databaseHistory, error) {
	info, err := historyguard.ReadLookupSnapshotInfo(db)
	if err != nil {
		return databaseHistory{}, err
	}
	if info != nil {
		return databaseHistory{Kind: "cx-lookup-snapshot", Report: info.SourceHistory, LookupCutoff: info.Cutoff}, nil
	}
	report, err := historyguard.Probe(db, name, 0, cutoff, probes)
	return databaseHistory{Kind: "chain", Report: report}, err
}

// coverageProblems lists every reason the run may not have seen all receipts.
// An empty list is the only state in which pending totals may be published.
func coverageProblems(
	databases map[uint32]databaseHistory,
	cutoffs map[uint32]uint64,
	sources []uint32,
	directions []directionTotals,
	coverageFrom uint64,
) []string {
	problems := []string{}
	if coverageFrom > historyguard.MainnetCrossTxFirstBlock {
		problems = append(problems, fmt.Sprintf(
			"receipt coverage starts at block %d, after the first cross-shard block %d",
			coverageFrom, historyguard.MainnetCrossTxFirstBlock))
	}
	isSource := make(map[uint32]bool)
	for _, shard := range sources {
		isSource[shard] = true
	}
	for shard := uint32(0); shard <= 1; shard++ {
		db := databases[shard]
		if isSource[shard] && db.Kind != "chain" {
			problems = append(problems, fmt.Sprintf(
				"shard %d database is a CX lookup snapshot and cannot supply outgoing receipts", shard))
		}
		if !db.Report.Complete {
			problems = append(problems, fmt.Sprintf(
				"shard %d %s lacks block history over %d-%d: snapdb marker %t, %d of %d probed blocks missing (first %v)",
				shard, db.Kind, db.Report.From, db.Report.To, db.Report.SnapDBMarker,
				db.Report.MissingProbes, db.Report.Probes, db.Report.MissingBlocks))
		}
		if db.Kind == "cx-lookup-snapshot" && db.LookupCutoff < cutoffs[shard] {
			problems = append(problems, fmt.Sprintf(
				"shard %d lookup snapshot stops at block %d, before the destination cutoff %d",
				shard, db.LookupCutoff, cutoffs[shard]))
		}
	}
	for _, direction := range directions {
		coverage := direction.SourceCoverage
		if !coverage.Complete {
			problems = append(problems, fmt.Sprintf(
				"shard %d has no outgoing receipt group toward shard %d for %d of %d blocks in %d-%d (gaps %v)",
				direction.SourceShard, coverage.Destination, coverage.BlocksMissing,
				coverage.BlocksExpected, coverage.From, coverage.To, coverage.MissingRanges))
		}
	}
	return problems
}

func main() {
	var (
		shard0Path      = flag.String("shard0-db", "", "path to shard-0 LevelDB")
		shard1Path      = flag.String("shard1-db", "", "path to shard-1 LevelDB")
		shard0Cutoff    = flag.Uint64("shard0-cutoff", 0, "last included canonical shard-0 block")
		shard1Cutoff    = flag.Uint64("shard1-cutoff", 0, "last included canonical shard-1 block")
		output          = flag.String("output", "", "JSON output path")
		cacheMB         = flag.Int("cache-mb", 128, "cache per LevelDB in MiB")
		handles         = flag.Int("handles", 128, "open-file handles per LevelDB")
		sourceShards    = flag.String("source-shards", "0,1", "source shards to scan; a shard given as a cx-lookup-snapshot can only be a destination")
		coverageFrom    = flag.Uint64("coverage-from", historyguard.MainnetCrossTxFirstBlock, "first source block that must have outgoing receipt groups")
		probes          = flag.Int("history-probes", historyguard.DefaultProbes, "evenly spaced blocks checked for canonical history in each database")
		allowIncomplete = flag.Bool("allow-incomplete-history", false, "write the output even when history or receipt coverage is incomplete; it is marked coverage_complete=false")
	)
	flag.Parse()
	if *shard0Path == "" || *shard1Path == "" || *shard0Cutoff == 0 || *shard1Cutoff == 0 || *output == "" {
		flag.Usage()
		os.Exit(2)
	}
	sources, err := parseSourceShards(*sourceShards)
	if err != nil {
		fatalf("%v", err)
	}

	shard0 := openDatabase(*shard0Path, *cacheMB, *handles)
	defer shard0.Close()
	shard1 := openDatabase(*shard1Path, *cacheMB, *handles)
	defer shard1.Close()

	cutoffs := map[uint32]uint64{0: *shard0Cutoff, 1: *shard1Cutoff}
	dbs := map[uint32]ethdb.Database{0: shard0, 1: shard1}
	paths := map[uint32]string{0: *shard0Path, 1: *shard1Path}
	histories := make(map[uint32]databaseHistory)
	for shard, db := range dbs {
		history, err := inspectDatabase(paths[shard], db, cutoffs[shard], *probes)
		if err != nil {
			fatalf("inspect shard %d database history: %v", shard, err)
		}
		histories[shard] = history
	}

	var directions []directionTotals
	for _, shard := range sources {
		destination := 1 - shard
		directions = append(directions, sumReceipts(
			shard,
			dbs[shard],
			map[uint32]ethdb.Database{destination: dbs[destination]},
			cutoffs[shard],
			cutoffs,
			*coverageFrom,
		))
	}
	problems := coverageProblems(histories, cutoffs, sources, directions, *coverageFrom)
	if len(problems) > 0 && !*allowIncomplete {
		for _, problem := range problems {
			fmt.Fprintf(os.Stderr, "incomplete: %s\n", problem)
		}
		fatalf("history or receipt coverage is incomplete; no totals written (use an archive database, or -allow-incomplete-history for a marked diagnostic run)")
	}

	pending := new(big.Int)
	unsupported := new(big.Int)
	for _, direction := range directions {
		value, ok := new(big.Int).SetString(direction.PendingAmountAtto, 10)
		if !ok {
			fatalf("invalid pending amount")
		}
		pending.Add(pending, value)
		value, ok = new(big.Int).SetString(direction.UnsupportedAmountAtto, 10)
		if !ok {
			fatalf("invalid unsupported amount")
		}
		unsupported.Add(unsupported, value)
	}
	result := summary{
		Shard0DB:                 *shard0Path,
		Shard1DB:                 *shard1Path,
		Shard0Cutoff:             *shard0Cutoff,
		Shard1Cutoff:             *shard1Cutoff,
		SourceShards:             sources,
		CoverageFrom:             *coverageFrom,
		Databases:                map[string]databaseHistory{"shard0": histories[0], "shard1": histories[1]},
		CoverageComplete:         len(problems) == 0,
		IncompleteHistoryAllowed: *allowIncomplete,
		CoverageProblems:         problems,
		Directions:               directions,
		PendingActiveAtto:        pending.String(),
		UnsupportedPendingAtto:   unsupported.String(),
	}

	partial := *output + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		file.Close()
		os.Remove(partial)
		fatalf("encode output: %v", err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		os.Remove(partial)
		fatalf("sync output: %v", err)
	}
	if err := file.Close(); err != nil {
		os.Remove(partial)
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, *output); err != nil {
		os.Remove(partial)
		fatalf("publish output: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode stdout: %v", err)
	}
}
