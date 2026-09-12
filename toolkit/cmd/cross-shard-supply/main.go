package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
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
	SourceShard                uint32         `json:"source_shard"`
	EmptyReceiptGroups         uint64         `json:"empty_receipt_groups"`
	CanonicalReceiptGroups     uint64         `json:"canonical_receipt_groups"`
	NonCanonicalReceiptGroups  uint64         `json:"noncanonical_receipt_groups"`
	MissingCanonicalHashGroups uint64         `json:"missing_canonical_hash_groups"`
	AfterCutoffGroups          uint64         `json:"after_cutoff_groups"`
	SpentReceiptGroups         uint64         `json:"spent_receipt_groups"`
	SpentReceiptCount          uint64         `json:"spent_receipt_count"`
	SpentAmountAtto            string         `json:"spent_amount_atto"`
	PendingReceiptGroups       uint64         `json:"pending_receipt_groups"`
	PendingReceiptCount        uint64         `json:"pending_receipt_count"`
	PendingAmountAtto          string         `json:"pending_amount_atto"`
	UnsupportedReceiptGroups   uint64         `json:"unsupported_receipt_groups"`
	UnsupportedReceiptCount    uint64         `json:"unsupported_receipt_count"`
	UnsupportedAmountAtto      string         `json:"unsupported_amount_atto"`
	PendingGroups              []receiptGroup `json:"pending_groups"`
	UnsupportedGroups          []receiptGroup `json:"unsupported_groups"`
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

type summary struct {
	Shard0DB               string            `json:"shard0_db"`
	Shard1DB               string            `json:"shard1_db"`
	Shard0Cutoff           uint64            `json:"shard0_cutoff"`
	Shard1Cutoff           uint64            `json:"shard1_cutoff"`
	Directions             []directionTotals `json:"directions"`
	PendingActiveAtto      string            `json:"pending_active_atto"`
	UnsupportedPendingAtto string            `json:"unsupported_pending_atto"`
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

func sumReceipts(
	sourceShard uint32,
	source ethdb.Database,
	destinations map[uint32]ethdb.Database,
	sourceCutoff uint64,
	destinationCutoffs map[uint32]uint64,
) directionTotals {
	result := &mutableDirectionTotals{
		directionTotals:   directionTotals{SourceShard: sourceShard},
		spentAmount:       new(big.Int),
		pendingAmount:     new(big.Int),
		unsupportedAmount: new(big.Int),
	}
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
		if bytes.Equal(iterator.Value(), []byte{0xc0}) {
			result.EmptyReceiptGroups++
			continue
		}
		if blockNumber > sourceCutoff {
			result.AfterCutoffGroups++
			continue
		}
		canonicalHash := rawdb.ReadCanonicalHash(source, blockNumber)
		if canonicalHash == (common.Hash{}) {
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
			encoded, err := destinationDB.Get(cxLookupKey(receipt.TxHash))
			if err != nil || len(encoded) == 0 {
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
			if rawdb.ReadCanonicalHash(destinationDB, entry.BlockIndex) != entry.BlockHash {
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

func main() {
	var (
		shard0Path   = flag.String("shard0-db", "", "path to shard-0 LevelDB")
		shard1Path   = flag.String("shard1-db", "", "path to shard-1 LevelDB")
		shard0Cutoff = flag.Uint64("shard0-cutoff", 0, "last included canonical shard-0 block")
		shard1Cutoff = flag.Uint64("shard1-cutoff", 0, "last included canonical shard-1 block")
		output       = flag.String("output", "", "JSON output path")
		cacheMB      = flag.Int("cache-mb", 128, "cache per LevelDB in MiB")
		handles      = flag.Int("handles", 128, "open-file handles per LevelDB")
	)
	flag.Parse()
	if *shard0Path == "" || *shard1Path == "" || *shard0Cutoff == 0 || *shard1Cutoff == 0 || *output == "" {
		flag.Usage()
		os.Exit(2)
	}

	shard0 := openDatabase(*shard0Path, *cacheMB, *handles)
	defer shard0.Close()
	shard1 := openDatabase(*shard1Path, *cacheMB, *handles)
	defer shard1.Close()

	cutoffs := map[uint32]uint64{0: *shard0Cutoff, 1: *shard1Cutoff}
	direction0 := sumReceipts(
		0,
		shard0,
		map[uint32]ethdb.Database{1: shard1},
		*shard0Cutoff,
		cutoffs,
	)
	direction1 := sumReceipts(
		1,
		shard1,
		map[uint32]ethdb.Database{0: shard0},
		*shard1Cutoff,
		cutoffs,
	)
	pending := new(big.Int)
	unsupported := new(big.Int)
	for _, direction := range []directionTotals{direction0, direction1} {
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
		Shard0DB:               *shard0Path,
		Shard1DB:               *shard1Path,
		Shard0Cutoff:           *shard0Cutoff,
		Shard1Cutoff:           *shard1Cutoff,
		Directions:             []directionTotals{direction0, direction1},
		PendingActiveAtto:      pending.String(),
		UnsupportedPendingAtto: unsupported.String(),
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
