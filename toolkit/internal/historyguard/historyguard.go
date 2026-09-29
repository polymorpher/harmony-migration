// Package historyguard checks that a Harmony LevelDB holds the block history a
// scan depends on. Prefix and index scans cannot tell an empty result from
// data the database never stored: a compact SnapDB (Harmony's dumpdb output)
// keeps every balance but no historical blocks, outgoing cross-shard receipts
// or CX lookups, so a scan of it finds only what the node processed after the
// snapshot.
package historyguard

import (
	"encoding/binary"
	"encoding/json"
	"fmt"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/ethdb"
)

// MainnetCrossTxFirstBlock is the first mainnet block that stores outgoing
// cross-shard receipt groups. CrossTxEpoch is 28
// (internal/params/config.go) and epoch 1 begins at block 344,064 with
// 16,384-block epochs (internal/configs/sharding/mainnet.go), so epoch 28
// begins at 344,064 + 27 * 16,384.
const MainnetCrossTxFirstBlock uint64 = 786432

// DefaultProbes is the number of evenly spaced blocks Probe checks.
const DefaultProbes = 1000

const maxListed = 20

var (
	snapdbInfoKey = []byte("SnapdbInfo")
	// LookupSnapshotKey holds the provenance of a minimal CX lookup database
	// written by cx-lookup-snapshot.
	LookupSnapshotKey = []byte("harmony-migration/cx-lookup-snapshot")
)

// Report describes whether a database holds canonical block history over a
// block range.
type Report struct {
	DB            string   `json:"db"`
	SnapDBMarker  bool     `json:"snapdb_marker"`
	From          uint64   `json:"from_block"`
	To            uint64   `json:"to_block"`
	Probes        int      `json:"probes"`
	MissingProbes int      `json:"missing_probes"`
	MissingBlocks []uint64 `json:"missing_probe_blocks"`
	Complete      bool     `json:"complete"`
}

// Coverage describes which blocks in a range have an outgoing receipt group
// toward one destination shard.
type Coverage struct {
	Destination    uint32      `json:"destination_shard"`
	From           uint64      `json:"from_block"`
	To             uint64      `json:"to_block"`
	FirstBlock     uint64      `json:"first_block_with_group"`
	LastBlock      uint64      `json:"last_block_with_group"`
	BlocksExpected uint64      `json:"blocks_expected"`
	BlocksPresent  uint64      `json:"blocks_present"`
	BlocksMissing  uint64      `json:"blocks_missing"`
	MissingRanges  [][2]uint64 `json:"missing_ranges"`
	Complete       bool        `json:"complete"`
}

// LookupSnapshotInfo is stored under LookupSnapshotKey.
type LookupSnapshotInfo struct {
	SourceDB          string `json:"source_db"`
	Cutoff            uint64 `json:"cutoff_block"`
	SourceHistory     Report `json:"source_history"`
	IncompleteAllowed bool   `json:"incomplete_history_allowed"`
}

func canonicalHashKey(number uint64) []byte {
	key := make([]byte, 10)
	key[0] = 'h'
	binary.BigEndian.PutUint64(key[1:9], number)
	key[9] = 'n'
	return key
}

func numberHashKey(prefix byte, number uint64, hash common.Hash) []byte {
	key := make([]byte, 1+8+common.HashLength)
	key[0] = prefix
	binary.BigEndian.PutUint64(key[1:9], number)
	copy(key[9:], hash.Bytes())
	return key
}

func has(db ethdb.KeyValueReader, key []byte) (bool, error) {
	present, err := db.Has(key)
	if err != nil {
		return false, fmt.Errorf("check key %x: %w", key, err)
	}
	return present, nil
}

// HasSnapDBMarker reports whether the database was created by Harmony's dumpdb.
func HasSnapDBMarker(db ethdb.KeyValueReader) (bool, error) {
	return has(db, snapdbInfoKey)
}

func blockPresent(db ethdb.KeyValueReader, number uint64) (bool, error) {
	key := canonicalHashKey(number)
	present, err := has(db, key)
	if err != nil || !present {
		return false, err
	}
	encoded, err := db.Get(key)
	if err != nil {
		return false, fmt.Errorf("read canonical hash %d: %w", number, err)
	}
	if len(encoded) != common.HashLength {
		return false, fmt.Errorf("canonical hash %d has %d bytes", number, len(encoded))
	}
	hash := common.BytesToHash(encoded)
	for _, prefix := range []byte{'h', 'b'} {
		present, err := has(db, numberHashKey(prefix, number, hash))
		if err != nil || !present {
			return false, err
		}
	}
	return true, nil
}

func probeBlocks(from, to uint64, probes int) []uint64 {
	if to < from {
		return nil
	}
	if probes < 2 {
		probes = 2
	}
	span := to - from
	if span < uint64(probes) {
		probes = int(span) + 1
	}
	if probes == 1 {
		return []uint64{from}
	}
	blocks := make([]uint64, probes)
	for i := range blocks {
		blocks[i] = from + span*uint64(i)/uint64(probes-1)
	}
	return blocks
}

// Probe checks the SnapDB marker and, at evenly spaced blocks from from to to,
// that a canonical hash, header and body are all stored. A database that
// passes has not been reduced to a snapshot over that range; it does not prove
// every auxiliary index is complete.
func Probe(db ethdb.KeyValueReader, name string, from, to uint64, probes int) (Report, error) {
	report := Report{DB: name, From: from, To: to, MissingBlocks: []uint64{}}
	marker, err := HasSnapDBMarker(db)
	if err != nil {
		return report, err
	}
	report.SnapDBMarker = marker
	for _, number := range probeBlocks(from, to, probes) {
		report.Probes++
		present, err := blockPresent(db, number)
		if err != nil {
			return report, err
		}
		if !present {
			report.MissingProbes++
			if len(report.MissingBlocks) < maxListed {
				report.MissingBlocks = append(report.MissingBlocks, number)
			}
		}
	}
	report.Complete = !report.SnapDBMarker && report.MissingProbes == 0
	return report, nil
}

// CoverageTracker counts, for one destination shard, the source blocks in a
// range that have an outgoing receipt group. Harmony writes a group (empty or
// not) toward every other shard for every block once cross-shard fields are
// active, so a complete database has one for every block in the range.
type CoverageTracker struct {
	destination uint32
	from, to    uint64
	next        uint64
	last        uint64
	seen        bool
	present     uint64
	missing     uint64
	first       uint64
	lastAny     uint64
	ranges      [][2]uint64
}

// NewCoverageTracker tracks groups toward destination for blocks from..to.
func NewCoverageTracker(destination uint32, from, to uint64) *CoverageTracker {
	return &CoverageTracker{destination: destination, from: from, to: to, next: from}
}

func (t *CoverageTracker) gap(start, end uint64) {
	t.missing += end - start + 1
	if len(t.ranges) < maxListed {
		t.ranges = append(t.ranges, [2]uint64{start, end})
	}
}

// Observe records a receipt group key. Keys must arrive in database order,
// which sorts by destination and then by block number.
func (t *CoverageTracker) Observe(destination uint32, number uint64) error {
	if destination != t.destination {
		return nil
	}
	if t.first == 0 || number < t.first {
		t.first = number
	}
	if number > t.lastAny {
		t.lastAny = number
	}
	if number < t.from || number > t.to {
		return nil
	}
	if t.seen && number < t.last {
		return fmt.Errorf("receipt group for block %d follows block %d", number, t.last)
	}
	if t.seen && number == t.last {
		return nil
	}
	if number > t.next {
		t.gap(t.next, number-1)
	}
	t.present++
	t.next = number + 1
	t.last = number
	t.seen = true
	return nil
}

// Finish returns the coverage over the whole range.
func (t *CoverageTracker) Finish() Coverage {
	result := Coverage{
		Destination: t.destination,
		From:        t.from,
		To:          t.to,
		FirstBlock:  t.first,
		LastBlock:   t.lastAny,
	}
	if t.to >= t.from {
		result.BlocksExpected = t.to - t.from + 1
		if t.next <= t.to {
			t.gap(t.next, t.to)
		}
	}
	result.BlocksPresent = t.present
	result.BlocksMissing = t.missing
	result.MissingRanges = t.ranges
	if result.MissingRanges == nil {
		result.MissingRanges = [][2]uint64{}
	}
	result.Complete = result.BlocksMissing == 0
	return result
}

// ReadLookupSnapshotInfo returns the provenance stored by cx-lookup-snapshot,
// or nil when the database is not such a snapshot.
func ReadLookupSnapshotInfo(db ethdb.KeyValueReader) (*LookupSnapshotInfo, error) {
	present, err := has(db, LookupSnapshotKey)
	if err != nil || !present {
		return nil, err
	}
	encoded, err := db.Get(LookupSnapshotKey)
	if err != nil {
		return nil, fmt.Errorf("read lookup snapshot provenance: %w", err)
	}
	var info LookupSnapshotInfo
	if err := json.Unmarshal(encoded, &info); err != nil {
		return nil, fmt.Errorf("decode lookup snapshot provenance: %w", err)
	}
	return &info, nil
}

// WriteLookupSnapshotInfo stores the provenance of a lookup snapshot.
func WriteLookupSnapshotInfo(db ethdb.KeyValueWriter, info LookupSnapshotInfo) error {
	encoded, err := json.Marshal(info)
	if err != nil {
		return err
	}
	return db.Put(LookupSnapshotKey, encoded)
}
