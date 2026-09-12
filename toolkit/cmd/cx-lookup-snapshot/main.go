package main

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
)

var cxLookupPrefix = []byte("cx")

type txLookupEntry struct {
	BlockHash  common.Hash
	BlockIndex uint64
	Index      uint64
}

type summary struct {
	SourceDB            string `json:"source_db"`
	OutputDB            string `json:"output_db"`
	CutoffBlock         uint64 `json:"cutoff_block"`
	KeysScanned         uint64 `json:"keys_scanned"`
	LookupEntries       uint64 `json:"lookup_entries"`
	AfterCutoff         uint64 `json:"after_cutoff"`
	NonCanonical        uint64 `json:"noncanonical"`
	ExportedLookups     uint64 `json:"exported_lookups"`
	ExportedBlockHashes uint64 `json:"exported_block_hashes"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func canonicalHashKey(number uint64) []byte {
	key := make([]byte, 1+8+1)
	key[0] = 'h'
	binary.BigEndian.PutUint64(key[1:9], number)
	key[9] = 'n'
	return key
}

func main() {
	var (
		sourcePath = flag.String("source-db", "", "source Harmony LevelDB")
		outputPath = flag.String("output-db", "", "new minimal CX lookup LevelDB")
		cutoff     = flag.Uint64("cutoff", 0, "maximum destination block")
		cacheMB    = flag.Int("cache-mb", 128, "source LevelDB cache in MiB")
		handles    = flag.Int("handles", 128, "source LevelDB handles")
	)
	flag.Parse()
	if *sourcePath == "" || *outputPath == "" || *cutoff == 0 {
		flag.Usage()
		os.Exit(2)
	}
	if _, err := os.Stat(*outputPath); !os.IsNotExist(err) {
		fatalf("output path already exists or cannot be stated: %v", err)
	}

	sourceDisk, err := leveldb.New(*sourcePath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open source database: %v", err)
	}
	source := rawdb.NewDatabase(sourceDisk)
	defer source.Close()
	output, err := leveldb.New(*outputPath, 16, 64, "", false)
	if err != nil {
		fatalf("create output database: %v", err)
	}

	result := summary{
		SourceDB:    *sourcePath,
		OutputDB:    *outputPath,
		CutoffBlock: *cutoff,
	}
	exportedBlocks := make(map[uint64]common.Hash)
	batch := output.NewBatch()
	iterator := source.NewIterator(cxLookupPrefix, nil)
	for iterator.Next() {
		result.KeysScanned++
		key := iterator.Key()
		if len(key) != len(cxLookupPrefix)+common.HashLength {
			continue
		}
		result.LookupEntries++
		var entry txLookupEntry
		if err := rlp.DecodeBytes(iterator.Value(), &entry); err != nil {
			iterator.Release()
			output.Close()
			fatalf("decode lookup key %s: %v", hex.EncodeToString(key), err)
		}
		if entry.BlockIndex > *cutoff {
			result.AfterCutoff++
			continue
		}
		if rawdb.ReadCanonicalHash(source, entry.BlockIndex) != entry.BlockHash {
			result.NonCanonical++
			continue
		}
		if prior, ok := exportedBlocks[entry.BlockIndex]; ok && prior != entry.BlockHash {
			iterator.Release()
			output.Close()
			fatalf("conflicting canonical hashes at block %d", entry.BlockIndex)
		}
		exportedBlocks[entry.BlockIndex] = entry.BlockHash
		if err := batch.Put(common.CopyBytes(key), common.CopyBytes(iterator.Value())); err != nil {
			iterator.Release()
			output.Close()
			fatalf("batch lookup: %v", err)
		}
		result.ExportedLookups++
		if batch.ValueSize() >= 4*1024*1024 {
			if err := batch.Write(); err != nil {
				iterator.Release()
				output.Close()
				fatalf("write lookup batch: %v", err)
			}
			batch.Reset()
		}
	}
	if err := iterator.Error(); err != nil {
		iterator.Release()
		output.Close()
		fatalf("iterate lookup entries: %v", err)
	}
	iterator.Release()
	for number, hash := range exportedBlocks {
		if err := batch.Put(canonicalHashKey(number), hash.Bytes()); err != nil {
			output.Close()
			fatalf("batch canonical hash: %v", err)
		}
		result.ExportedBlockHashes++
		if batch.ValueSize() >= 4*1024*1024 {
			if err := batch.Write(); err != nil {
				output.Close()
				fatalf("write canonical hash batch: %v", err)
			}
			batch.Reset()
		}
	}
	if batch.ValueSize() != 0 {
		if err := batch.Write(); err != nil {
			output.Close()
			fatalf("write final batch: %v", err)
		}
	}
	if err := output.Close(); err != nil {
		fatalf("close output database: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}
