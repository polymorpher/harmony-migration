package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"

	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
)

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func main() {
	var (
		dbPath      = flag.String("db", "", "path to shard-0 LevelDB")
		blockNumber = flag.Uint64("block", 0, "block number")
	)
	flag.Parse()
	if *dbPath == "" || *blockNumber == 0 {
		flag.Usage()
		os.Exit(2)
	}

	disk, err := leveldb.New(*dbPath, 64, 64, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()

	key := make([]byte, len("blk-rwd-")+8)
	copy(key, []byte("blk-rwd-"))
	binary.BigEndian.PutUint64(key[len("blk-rwd-"):], *blockNumber)
	encoded, err := db.Get(key)
	if err != nil {
		fatalf("read block reward accumulator: %v", err)
	}
	result := struct {
		BlockNumber uint64 `json:"block_number"`
		RewardAtto  string `json:"reward_atto"`
	}{
		BlockNumber: *blockNumber,
		RewardAtto:  new(big.Int).SetBytes(encoded).String(),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode result: %v", err)
	}
}
