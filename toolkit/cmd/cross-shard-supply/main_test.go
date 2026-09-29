package main

import (
	"encoding/binary"
	"errors"
	"math/big"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/rlp"

	"github.com/polymorpher/harmony-migration/toolkit/internal/historyguard"
)

type lookupReader struct {
	has      bool
	hasErr   error
	value    []byte
	valueErr error
}

func (reader lookupReader) Has([]byte) (bool, error) {
	return reader.has, reader.hasErr
}

func (reader lookupReader) Get([]byte) ([]byte, error) {
	return reader.value, reader.valueErr
}

func TestReadCXLookupDistinguishesMissingAndFailedReads(t *testing.T) {
	hash := common.HexToHash("0x01")
	if value, applied, err := readCXLookup(
		lookupReader{},
		hash,
	); err != nil || applied || value != nil {
		t.Fatalf("missing lookup = %x, %v, %v", value, applied, err)
	}
	if _, _, err := readCXLookup(
		lookupReader{hasErr: errors.New("closed")},
		hash,
	); err == nil {
		t.Fatal("presence read error was accepted")
	}
	if _, _, err := readCXLookup(
		lookupReader{has: true, valueErr: errors.New("I/O")},
		hash,
	); err == nil {
		t.Fatal("value read error was accepted")
	}
	if _, _, err := readCXLookup(
		lookupReader{has: true},
		hash,
	); err == nil {
		t.Fatal("empty present lookup was accepted")
	}
	expected := []byte{0xc0}
	value, applied, err := readCXLookup(
		lookupReader{has: true, value: expected},
		hash,
	)
	if err != nil || !applied || string(value) != string(expected) {
		t.Fatalf("existing lookup = %x, %v, %v", value, applied, err)
	}
}

func TestReadCanonicalHashDistinguishesMissingAndFailedReads(t *testing.T) {
	if hash, exists, err := readCanonicalHash(
		lookupReader{},
		7,
	); err != nil || exists || hash != (common.Hash{}) {
		t.Fatalf("missing hash = %s, %v, %v", hash, exists, err)
	}
	if _, _, err := readCanonicalHash(
		lookupReader{hasErr: errors.New("closed")},
		7,
	); err == nil {
		t.Fatal("canonical presence read error was accepted")
	}
	if _, _, err := readCanonicalHash(
		lookupReader{has: true, valueErr: errors.New("I/O")},
		7,
	); err == nil {
		t.Fatal("canonical value read error was accepted")
	}
	expected := common.HexToHash("0x1234")
	hash, exists, err := readCanonicalHash(
		lookupReader{has: true, value: expected.Bytes()},
		7,
	)
	if err != nil || !exists || hash != expected {
		t.Fatalf("canonical hash = %s, %v, %v", hash, exists, err)
	}
}

func blockHash(shard uint32, number uint64) common.Hash {
	return common.BigToHash(new(big.Int).SetUint64(uint64(shard)<<40 | number))
}

func put(t *testing.T, db ethdb.KeyValueWriter, key, value []byte) {
	t.Helper()
	if err := db.Put(key, value); err != nil {
		t.Fatal(err)
	}
}

func receiptGroupKey(destination uint32, number uint64, hash common.Hash) []byte {
	key := append([]byte{}, receiptPrefix...)
	key = binary.BigEndian.AppendUint32(key, destination)
	key = binary.BigEndian.AppendUint64(key, number)
	return append(key, hash.Bytes()...)
}

func TestSumReceiptsReportsSourceCoverageGaps(t *testing.T) {
	source := rawdb.NewMemoryDatabase()
	destination := rawdb.NewMemoryDatabase()
	recipient := common.HexToAddress("0x02")
	txHash := common.HexToHash("0xaa")
	for number := uint64(1); number <= 10; number++ {
		hash := blockHash(0, number)
		put(t, source, canonicalHashKey(number), hash.Bytes())
		put(t, destination, canonicalHashKey(number), blockHash(1, number).Bytes())
		if number == 4 || number == 5 {
			continue
		}
		value := []byte{0xc0}
		if number == 7 {
			encoded, err := rlp.EncodeToBytes([]*cxReceipt{{
				TxHash: txHash, To: &recipient, ShardID: 0, ToShardID: 1, Amount: big.NewInt(9),
			}})
			if err != nil {
				t.Fatal(err)
			}
			value = encoded
		}
		put(t, source, receiptGroupKey(1, number, hash), value)
	}
	lookup, err := rlp.EncodeToBytes(txLookupEntry{BlockHash: blockHash(1, 8), BlockIndex: 8})
	if err != nil {
		t.Fatal(err)
	}
	put(t, destination, cxLookupKey(txHash), lookup)

	totals := sumReceipts(0, source, map[uint32]ethdb.Database{1: destination}, 10, map[uint32]uint64{0: 10, 1: 10}, 1)
	coverage := totals.SourceCoverage
	if coverage.Complete || coverage.BlocksMissing != 2 || len(coverage.MissingRanges) != 1 || coverage.MissingRanges[0] != [2]uint64{4, 5} {
		t.Fatalf("coverage = %+v", coverage)
	}
	if totals.SpentReceiptCount != 1 || totals.PendingReceiptCount != 0 || totals.EmptyReceiptGroups != 7 {
		t.Fatalf("totals = %+v", totals)
	}
}

func completeHistory() map[uint32]databaseHistory {
	report := historyguard.Report{Complete: true, Probes: 10}
	return map[uint32]databaseHistory{
		0: {Kind: "chain", Report: report},
		1: {Kind: "chain", Report: report},
	}
}

func completeDirections() []directionTotals {
	return []directionTotals{
		{SourceShard: 0, SourceCoverage: historyguard.Coverage{Destination: 1, Complete: true}},
		{SourceShard: 1, SourceCoverage: historyguard.Coverage{Destination: 0, Complete: true}},
	}
}

func TestCoverageProblems(t *testing.T) {
	cutoffs := map[uint32]uint64{0: 100, 1: 100}
	from := historyguard.MainnetCrossTxFirstBlock
	if problems := coverageProblems(completeHistory(), cutoffs, []uint32{0, 1}, completeDirections(), from); len(problems) != 0 {
		t.Fatalf("complete evidence reported %v", problems)
	}

	compact := completeHistory()
	compact[0] = databaseHistory{Kind: "chain", Report: historyguard.Report{SnapDBMarker: true, MissingProbes: 9, Probes: 10}}
	gap := completeDirections()
	gap[0].SourceCoverage = historyguard.Coverage{Destination: 1, BlocksMissing: 5, BlocksExpected: 10}
	lookupSource := completeHistory()
	lookupSource[1] = databaseHistory{Kind: "cx-lookup-snapshot", Report: historyguard.Report{Complete: true}, LookupCutoff: 100}
	earlyLookup := completeHistory()
	earlyLookup[1] = databaseHistory{Kind: "cx-lookup-snapshot", Report: historyguard.Report{Complete: true}, LookupCutoff: 99}

	cases := []struct {
		name       string
		histories  map[uint32]databaseHistory
		sources    []uint32
		directions []directionTotals
		from       uint64
		want       string
	}{
		{"compact database", compact, []uint32{0, 1}, completeDirections(), from, "shard 0 chain lacks block history"},
		{"receipt gap", completeHistory(), []uint32{0, 1}, gap, from, "shard 0 has no outgoing receipt group toward shard 1"},
		{"lookup snapshot as source", lookupSource, []uint32{0, 1}, completeDirections(), from, "cannot supply outgoing receipts"},
		{"lookup snapshot too early", earlyLookup, []uint32{0}, completeDirections()[:1], from, "before the destination cutoff"},
		{"late coverage start", completeHistory(), []uint32{0, 1}, completeDirections(), from + 1, "after the first cross-shard block"},
	}
	for _, c := range cases {
		problems := coverageProblems(c.histories, cutoffs, c.sources, c.directions, c.from)
		if len(problems) != 1 || !strings.Contains(problems[0], c.want) {
			t.Fatalf("%s: problems = %v", c.name, problems)
		}
	}
	if problems := coverageProblems(lookupSource, cutoffs, []uint32{0}, completeDirections()[:1], from); len(problems) != 0 {
		t.Fatalf("lookup snapshot as destination reported %v", problems)
	}
}

func TestInspectDatabaseRecognizesLookupSnapshotsAndChains(t *testing.T) {
	snapshot := rawdb.NewMemoryDatabase()
	if err := historyguard.WriteLookupSnapshotInfo(snapshot, historyguard.LookupSnapshotInfo{
		Cutoff: 5, SourceHistory: historyguard.Report{Complete: true},
	}); err != nil {
		t.Fatal(err)
	}
	history, err := inspectDatabase("snapshot", snapshot, 5, 10)
	if err != nil || history.Kind != "cx-lookup-snapshot" || history.LookupCutoff != 5 || !history.Report.Complete {
		t.Fatalf("snapshot history = %+v, %v", history, err)
	}

	chain := rawdb.NewMemoryDatabase()
	for number := uint64(0); number <= 5; number++ {
		hash := blockHash(0, number)
		put(t, chain, canonicalHashKey(number), hash.Bytes())
		for _, prefix := range []byte{'h', 'b'} {
			key := binary.BigEndian.AppendUint64([]byte{prefix}, number)
			put(t, chain, append(key, hash.Bytes()...), []byte{0xc0})
		}
	}
	history, err = inspectDatabase("chain", chain, 5, 10)
	if err != nil || history.Kind != "chain" || !history.Report.Complete || history.Report.Probes != 6 {
		t.Fatalf("chain history = %+v, %v", history, err)
	}
}

func TestParseSourceShards(t *testing.T) {
	shards, err := parseSourceShards("1, 0,1")
	if err != nil || len(shards) != 2 || shards[0] != 1 || shards[1] != 0 {
		t.Fatalf("shards = %v, %v", shards, err)
	}
	if _, err := parseSourceShards("2"); err == nil {
		t.Fatal("retired shard accepted as a source")
	}
}
