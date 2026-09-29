package historyguard

import (
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/ethdb/memorydb"
)

func putBlock(t *testing.T, db *memorydb.Database, number uint64) {
	t.Helper()
	hash := common.BigToHash(new(big.Int).SetUint64(number + 1))
	for key, value := range map[string][]byte{
		string(canonicalHashKey(number)):         hash.Bytes(),
		string(numberHashKey('h', number, hash)): {0xc0},
		string(numberHashKey('b', number, hash)): {0xc0},
	} {
		if err := db.Put([]byte(key), value); err != nil {
			t.Fatal(err)
		}
	}
}

func TestProbeAcceptsCompleteHistory(t *testing.T) {
	db := memorydb.New()
	for number := uint64(1); number <= 500; number++ {
		putBlock(t, db, number)
	}
	report, err := Probe(db, "full", 1, 500, 50)
	if err != nil {
		t.Fatal(err)
	}
	if !report.Complete || report.MissingProbes != 0 || report.Probes != 50 {
		t.Fatalf("report = %+v", report)
	}
}

func TestProbeBlocksSpanTheRange(t *testing.T) {
	blocks := probeBlocks(0, 5, 10)
	if len(blocks) != 6 || blocks[0] != 0 || blocks[5] != 5 {
		t.Fatalf("short range probes = %v", blocks)
	}
	blocks = probeBlocks(1, 93623067, 1000)
	if len(blocks) != 1000 || blocks[0] != 1 || blocks[999] != 93623067 {
		t.Fatalf("long range probes = %d from %d to %d", len(blocks), blocks[0], blocks[len(blocks)-1])
	}
	for i := 1; i < len(blocks); i++ {
		if blocks[i] <= blocks[i-1] || blocks[i]-blocks[i-1] > 93623067/999+1 {
			t.Fatalf("probe %d at %d after %d", i, blocks[i], blocks[i-1])
		}
	}
	if blocks := probeBlocks(7, 7, 10); len(blocks) != 1 || blocks[0] != 7 {
		t.Fatalf("single block probes = %v", blocks)
	}
	if blocks := probeBlocks(8, 7, 10); blocks != nil {
		t.Fatalf("empty range probes = %v", blocks)
	}
}

func TestProbeRejectsSnapshotHistory(t *testing.T) {
	db := memorydb.New()
	for number := uint64(400); number <= 500; number++ {
		putBlock(t, db, number)
	}
	report, err := Probe(db, "compact", 1, 500, 50)
	if err != nil {
		t.Fatal(err)
	}
	if report.Complete || report.MissingProbes == 0 || report.MissingBlocks[0] != 1 {
		t.Fatalf("report = %+v", report)
	}
}

func TestProbeRejectsSnapDBMarker(t *testing.T) {
	db := memorydb.New()
	for number := uint64(1); number <= 10; number++ {
		putBlock(t, db, number)
	}
	if err := db.Put(snapdbInfoKey, []byte{0xc0}); err != nil {
		t.Fatal(err)
	}
	report, err := Probe(db, "marked", 1, 10, 10)
	if err != nil {
		t.Fatal(err)
	}
	if report.Complete || !report.SnapDBMarker || report.MissingProbes != 0 {
		t.Fatalf("report = %+v", report)
	}
}

func TestProbeRejectsMissingBody(t *testing.T) {
	db := memorydb.New()
	putBlock(t, db, 5)
	hash := common.BytesToHash(mustGet(t, db, canonicalHashKey(5)))
	if err := db.Delete(numberHashKey('b', 5, hash)); err != nil {
		t.Fatal(err)
	}
	report, err := Probe(db, "bodiless", 5, 5, 2)
	if err != nil {
		t.Fatal(err)
	}
	if report.Complete || report.MissingProbes != 1 {
		t.Fatalf("report = %+v", report)
	}
}

func mustGet(t *testing.T, db *memorydb.Database, key []byte) []byte {
	t.Helper()
	value, err := db.Get(key)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestCoverageCompleteWithDuplicatesAndOtherDestinations(t *testing.T) {
	tracker := NewCoverageTracker(1, 10, 14)
	for _, key := range [][2]uint64{{0, 3}, {1, 9}, {1, 10}, {1, 11}, {1, 11}, {1, 12}, {1, 13}, {1, 14}, {1, 20}, {2, 1}} {
		if err := tracker.Observe(uint32(key[0]), key[1]); err != nil {
			t.Fatal(err)
		}
	}
	coverage := tracker.Finish()
	if !coverage.Complete || coverage.BlocksPresent != 5 || coverage.FirstBlock != 9 || coverage.LastBlock != 20 {
		t.Fatalf("coverage = %+v", coverage)
	}
}

func TestCoverageReportsLeadingInnerAndTrailingGaps(t *testing.T) {
	tracker := NewCoverageTracker(1, 1, 100)
	for _, number := range []uint64{30, 31, 32, 50, 51} {
		if err := tracker.Observe(1, number); err != nil {
			t.Fatal(err)
		}
	}
	coverage := tracker.Finish()
	want := [][2]uint64{{1, 29}, {33, 49}, {52, 100}}
	if coverage.Complete || coverage.BlocksMissing != 95 || len(coverage.MissingRanges) != len(want) {
		t.Fatalf("coverage = %+v", coverage)
	}
	for i, gap := range want {
		if coverage.MissingRanges[i] != gap {
			t.Fatalf("gap %d = %v, want %v", i, coverage.MissingRanges[i], gap)
		}
	}
}

func TestCoverageRejectsOutOfOrderKeys(t *testing.T) {
	tracker := NewCoverageTracker(0, 1, 10)
	if err := tracker.Observe(0, 5); err != nil {
		t.Fatal(err)
	}
	if err := tracker.Observe(0, 4); err == nil {
		t.Fatal("out-of-order key was accepted")
	}
}

func TestCoverageOfEmptyRange(t *testing.T) {
	coverage := NewCoverageTracker(1, 10, 9).Finish()
	if !coverage.Complete || coverage.BlocksExpected != 0 {
		t.Fatalf("coverage = %+v", coverage)
	}
}

func TestLookupSnapshotInfoRoundTrip(t *testing.T) {
	db := memorydb.New()
	if info, err := ReadLookupSnapshotInfo(db); err != nil || info != nil {
		t.Fatalf("missing info = %+v, %v", info, err)
	}
	want := LookupSnapshotInfo{SourceDB: "/path/to/shard1-db", Cutoff: 42, SourceHistory: Report{Complete: true}}
	if err := WriteLookupSnapshotInfo(db, want); err != nil {
		t.Fatal(err)
	}
	got, err := ReadLookupSnapshotInfo(db)
	if err != nil || got == nil || got.SourceDB != want.SourceDB || got.Cutoff != 42 || !got.SourceHistory.Complete {
		t.Fatalf("info = %+v, %v", got, err)
	}
}

func TestMainnetCrossTxFirstBlock(t *testing.T) {
	const epochBlock1, blocksPerEpoch, crossTxEpoch = 344064, 16384, 28
	if MainnetCrossTxFirstBlock != epochBlock1+(crossTxEpoch-1)*blocksPerEpoch {
		t.Fatal("mainnet cross-shard start block does not match the epoch schedule")
	}
}
