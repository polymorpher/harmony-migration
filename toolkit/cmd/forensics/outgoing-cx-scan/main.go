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
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
)

var receiptPrefix = []byte("cxReceipt")

type cxReceipt struct {
	TxHash    common.Hash
	From      common.Address
	To        *common.Address
	ShardID   uint32
	ToShardID uint32
	Amount    *big.Int
}

type destinationSummary struct {
	DestinationShard uint32 `json:"destination_shard"`
	ReceiptGroups    uint64 `json:"receipt_groups"`
	ReceiptCount     uint64 `json:"receipt_count"`
	AmountAtto       string `json:"amount_atto"`
}

type summary struct {
	DBPath                    string               `json:"db_path"`
	SourceShard               uint32               `json:"source_shard"`
	CanonicalGroups           uint64               `json:"canonical_groups"`
	NoncanonicalGroups        uint64               `json:"noncanonical_groups"`
	MissingCanonicalHashGroup uint64               `json:"missing_canonical_hash_groups"`
	EmptyGroups               uint64               `json:"empty_groups"`
	Destinations              []destinationSummary `json:"destinations"`
	OutputPath                string               `json:"output_path"`
	OutputSHA256              string               `json:"output_sha256"`
	ElapsedMilliseconds       int64                `json:"elapsed_milliseconds"`
}

type mutableDestination struct {
	groups   uint64
	receipts uint64
	amount   *big.Int
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func receiptKeyParts(key []byte) (uint32, uint64, common.Hash, bool) {
	const suffixLength = 4 + 8 + common.HashLength
	if len(key) != len(receiptPrefix)+suffixLength || !bytes.HasPrefix(key, receiptPrefix) {
		return 0, 0, common.Hash{}, false
	}
	offset := len(receiptPrefix)
	return binary.BigEndian.Uint32(key[offset : offset+4]),
		binary.BigEndian.Uint64(key[offset+4 : offset+12]),
		common.BytesToHash(key[offset+12:]),
		true
}

func main() {
	var (
		dbPath      = flag.String("db", "", "path to source-shard LevelDB")
		sourceShard = flag.Uint("source-shard", 0, "source shard ID")
		output      = flag.String("output", "", "CSV output path")
		cacheMB     = flag.Int("cache-mb", 1024, "LevelDB cache in MiB")
		handles     = flag.Int("handles", 1024, "LevelDB open-file handles")
	)
	flag.Parse()
	if *dbPath == "" || *output == "" || *sourceShard > ^uint(0)>>32 {
		flag.Usage()
		os.Exit(2)
	}

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()

	partial := *output + ".partial"
	outputFile, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	outputComplete := false
	defer func() {
		outputFile.Close()
		if !outputComplete {
			os.Remove(partial)
		}
	}()
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(io.MultiWriter(outputFile, hasher), 1024*1024)
	writer := csv.NewWriter(buffered)
	if err := writer.Write([]string{
		"source_shard",
		"destination_shard",
		"source_block",
		"source_block_hash",
		"receipt_index",
		"tx_hash",
		"from",
		"to",
		"amount_atto",
	}); err != nil {
		fatalf("write CSV header: %v", err)
	}

	started := time.Now()
	lastProgress := started
	destinations := make(map[uint32]*mutableDestination)
	var canonicalGroups, noncanonicalGroups, missingHashGroups, emptyGroups uint64
	iterator := db.NewIterator(receiptPrefix, nil)
	defer iterator.Release()
	for iterator.Next() {
		destination, number, hash, ok := receiptKeyParts(iterator.Key())
		if !ok {
			continue
		}
		canonicalHash := rawdb.ReadCanonicalHash(db, number)
		if canonicalHash == (common.Hash{}) {
			missingHashGroups++
			continue
		}
		if canonicalHash != hash {
			noncanonicalGroups++
			continue
		}
		if bytes.Equal(iterator.Value(), []byte{0xc0}) {
			emptyGroups++
			continue
		}
		var receipts []*cxReceipt
		if err := rlp.DecodeBytes(iterator.Value(), &receipts); err != nil {
			fatalf("decode receipt group block %d: %v", number, err)
		}
		canonicalGroups++
		totals := destinations[destination]
		if totals == nil {
			totals = &mutableDestination{amount: new(big.Int)}
			destinations[destination] = totals
		}
		totals.groups++
		for index, receipt := range receipts {
			if receipt == nil || receipt.To == nil || receipt.Amount == nil ||
				receipt.Amount.Sign() < 0 ||
				receipt.ShardID != uint32(*sourceShard) ||
				receipt.ToShardID != destination {
				fatalf("invalid receipt block %d index %d", number, index)
			}
			totals.receipts++
			totals.amount.Add(totals.amount, receipt.Amount)
			if err := writer.Write([]string{
				fmt.Sprintf("%d", *sourceShard),
				fmt.Sprintf("%d", destination),
				fmt.Sprintf("%d", number),
				hash.Hex(),
				fmt.Sprintf("%d", index),
				receipt.TxHash.Hex(),
				receipt.From.Hex(),
				receipt.To.Hex(),
				receipt.Amount.String(),
			}); err != nil {
				fatalf("write receipt: %v", err)
			}
		}
		if time.Since(lastProgress) >= 10*time.Second {
			fmt.Fprintf(
				os.Stderr,
				"progress groups=%d canonical=%d receipts=%d elapsed=%s\n",
				canonicalGroups+noncanonicalGroups+missingHashGroups+emptyGroups,
				canonicalGroups,
				totals.receipts,
				time.Since(started).Round(time.Second),
			)
			lastProgress = time.Now()
		}
	}
	if err := iterator.Error(); err != nil {
		fatalf("iterate outgoing receipts: %v", err)
	}

	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush output: %v", err)
	}
	if err := outputFile.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := outputFile.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, *output); err != nil {
		fatalf("publish output: %v", err)
	}
	outputComplete = true

	result := summary{
		DBPath:                    *dbPath,
		SourceShard:               uint32(*sourceShard),
		CanonicalGroups:           canonicalGroups,
		NoncanonicalGroups:        noncanonicalGroups,
		MissingCanonicalHashGroup: missingHashGroups,
		EmptyGroups:               emptyGroups,
		OutputPath:                *output,
		OutputSHA256:              hex.EncodeToString(hasher.Sum(nil)),
		ElapsedMilliseconds:       time.Since(started).Milliseconds(),
	}
	for destination := uint32(0); destination <= 3; destination++ {
		totals := destinations[destination]
		if totals == nil {
			continue
		}
		result.Destinations = append(result.Destinations, destinationSummary{
			DestinationShard: destination,
			ReceiptGroups:    totals.groups,
			ReceiptCount:     totals.receipts,
			AmountAtto:       totals.amount.String(),
		})
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}
