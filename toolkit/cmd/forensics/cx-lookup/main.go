package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
)

type hashes []common.Hash

func (values *hashes) String() string {
	items := make([]string, len(*values))
	for i, hash := range *values {
		items[i] = hash.Hex()
	}
	return strings.Join(items, ",")
}

func (values *hashes) Set(value string) error {
	if len(value) != 66 {
		return fmt.Errorf("invalid 32-byte hash %q", value)
	}
	*values = append(*values, common.HexToHash(value))
	return nil
}

type lookupEntry struct {
	BlockHash  common.Hash
	BlockIndex uint64
	Index      uint64
}

type result struct {
	TransactionHash common.Hash `json:"transaction_hash"`
	Found           bool        `json:"found"`
	BlockHash       common.Hash `json:"block_hash"`
	BlockNumber     uint64      `json:"block_number"`
	Index           uint64      `json:"index"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func main() {
	var txHashes hashes
	var (
		dbPath  = flag.String("db", "", "path to destination-shard LevelDB")
		cacheMB = flag.Int("cache-mb", 64, "LevelDB cache in MiB")
		handles = flag.Int("handles", 64, "LevelDB open-file handles")
	)
	flag.Var(&txHashes, "hash", "cross-shard transaction hash; repeatable")
	flag.Parse()
	if *dbPath == "" || len(txHashes) == 0 {
		flag.Usage()
		os.Exit(2)
	}

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()

	results := make([]result, 0, len(txHashes))
	for _, txHash := range txHashes {
		key := append([]byte("cx"), txHash.Bytes()...)
		encoded, err := db.Get(key)
		if err != nil || len(encoded) == 0 {
			results = append(results, result{TransactionHash: txHash})
			continue
		}
		var entry lookupEntry
		if err := rlp.DecodeBytes(encoded, &entry); err != nil {
			fatalf("decode lookup for %s: %v", txHash.Hex(), err)
		}
		results = append(results, result{
			TransactionHash: txHash,
			Found:           true,
			BlockHash:       entry.BlockHash,
			BlockNumber:     entry.BlockIndex,
			Index:           entry.Index,
		})
	}
	if err := json.NewEncoder(os.Stdout).Encode(results); err != nil {
		fatalf("encode results: %v", err)
	}
}
