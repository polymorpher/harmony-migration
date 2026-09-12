package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
)

type summary struct {
	DBPath                string `json:"db_path"`
	StateRoot             string `json:"state_root"`
	ThresholdAtto         string `json:"threshold_atto"`
	OutputPath            string `json:"output_path"`
	OutputSHA256          string `json:"output_sha256"`
	Accounts              uint64 `json:"accounts"`
	PositiveAccounts      uint64 `json:"positive_accounts"`
	MissingPreimages      uint64 `json:"missing_preimages"`
	QualifyingAccounts    uint64 `json:"qualifying_accounts"`
	QualifyingCodeLess    uint64 `json:"qualifying_code_less"`
	QualifyingCodeBearing uint64 `json:"qualifying_code_bearing"`
	TotalBalanceAtto      string `json:"total_balance_atto"`
	QualifyingBalanceAtto string `json:"qualifying_balance_atto"`
	ElapsedMilliseconds   int64  `json:"elapsed_milliseconds"`
	AccountsPerSecond     int64  `json:"accounts_per_second"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) common.Hash {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid 32-byte state root %q", value)
	}
	return common.BytesToHash(decoded)
}

func parsePositiveBig(value, name string) *big.Int {
	n, ok := new(big.Int).SetString(value, 10)
	if !ok || n.Sign() <= 0 {
		fatalf("invalid positive decimal %s %q", name, value)
	}
	return n
}

func main() {
	var (
		dbPath           = flag.String("db", "", "path to a Harmony shard LevelDB")
		rootText         = flag.String("root", "", "state root hash")
		thresholdText    = flag.String("threshold-atto", "", "strict balance threshold in atto ONE")
		outputPath       = flag.String("output", "", "positive-balance CSV output path")
		cacheMB          = flag.Int("cache-mb", 256, "LevelDB/trie cache in MiB")
		handles          = flag.Int("handles", 256, "LevelDB open-file handles")
		requirePreimages = flag.Bool("require-preimages", true, "fail if any positive account lacks an address preimage")
	)
	flag.Parse()

	if *dbPath == "" || *rootText == "" || *thresholdText == "" || *outputPath == "" {
		flag.Usage()
		os.Exit(2)
	}
	root := parseHash(*rootText)
	threshold := parsePositiveBig(*thresholdText, "threshold")

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()

	trieDB := trie.NewDatabase(db)
	stateTrie, err := trie.NewStateTrie(trie.StateTrieID(root), trieDB)
	if err != nil {
		fatalf("open state trie %s: %v", root.Hex(), err)
	}
	if got := stateTrie.Hash(); got != root {
		fatalf("opened trie root mismatch: got %s want %s", got.Hex(), root.Hex())
	}

	partialPath := *outputPath + ".partial"
	outputFile, err := os.OpenFile(partialPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create partial output: %v", err)
	}
	outputComplete := false
	defer func() {
		outputFile.Close()
		if !outputComplete {
			os.Remove(partialPath)
		}
	}()

	outputHash := sha256.New()
	bufferedOutput := bufio.NewWriterSize(outputFile, 1024*1024)
	csvOutput := csv.NewWriter(outputHashWriter{bufferedOutput, outputHash})
	if err := csvOutput.Write([]string{
		"secure_key",
		"address",
		"balance_atto",
		"nonce",
		"code_hash",
	}); err != nil {
		fatalf("write CSV header: %v", err)
	}

	var (
		accounts              uint64
		positiveAccounts      uint64
		missingPreimages      uint64
		qualifyingAccounts    uint64
		qualifyingCodeLess    uint64
		qualifyingCodeBearing uint64
		totalBalance          = new(big.Int)
		qualifyingBalance     = new(big.Int)
		started               = time.Now()
		lastProgress          = started
	)

	iter := trie.NewIterator(stateTrie.NodeIterator(nil))
	for iter.Next() {
		var account types.StateAccount
		if err := rlp.DecodeBytes(iter.Value, &account); err != nil {
			fatalf("decode account at secure key %x: %v", iter.Key, err)
		}
		if account.Balance == nil || account.Balance.Sign() < 0 {
			fatalf("invalid balance at secure key %x", iter.Key)
		}

		accounts++
		totalBalance.Add(totalBalance, account.Balance)
		if account.Balance.Sign() > 0 {
			positiveAccounts++
			address := ""
			addrBytes := rawdb.ReadPreimage(db, common.BytesToHash(iter.Key))
			if len(addrBytes) == 0 {
				missingPreimages++
			} else {
				if len(addrBytes) != common.AddressLength {
					fatalf("invalid preimage length %d at secure key %x", len(addrBytes), iter.Key)
				}
				if got := crypto.Keccak256Hash(addrBytes); got != common.BytesToHash(iter.Key) {
					fatalf("preimage hash mismatch at secure key %x", iter.Key)
				}
				address = common.BytesToAddress(addrBytes).Hex()
			}
			if err := csvOutput.Write([]string{
				"0x" + hex.EncodeToString(iter.Key),
				address,
				account.Balance.String(),
				fmt.Sprintf("%d", account.Nonce),
				"0x" + hex.EncodeToString(account.CodeHash),
			}); err != nil {
				fatalf("write account CSV: %v", err)
			}
		}
		if account.Balance.Cmp(threshold) > 0 {
			qualifyingAccounts++
			qualifyingBalance.Add(qualifyingBalance, account.Balance)
			if bytes.Equal(account.CodeHash, types.EmptyCodeHash.Bytes()) {
				qualifyingCodeLess++
			} else {
				qualifyingCodeBearing++
			}
		}
		if time.Since(lastProgress) >= 10*time.Second {
			elapsed := time.Since(started)
			fmt.Fprintf(
				os.Stderr,
				"progress accounts=%d positive=%d qualifying=%d missing_preimages=%d rate=%d/s key=%x\n",
				accounts,
				positiveAccounts,
				qualifyingAccounts,
				missingPreimages,
				int64(float64(accounts)/elapsed.Seconds()),
				iter.Key,
			)
			lastProgress = time.Now()
		}
	}
	if iter.Err != nil {
		fatalf("state trie iteration: %v", iter.Err)
	}

	csvOutput.Flush()
	if err := csvOutput.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := bufferedOutput.Flush(); err != nil {
		fatalf("flush output buffer: %v", err)
	}
	if err := outputFile.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := outputFile.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partialPath, *outputPath); err != nil {
		fatalf("publish output: %v", err)
	}
	outputComplete = true

	elapsed := time.Since(started)
	result := summary{
		DBPath:                *dbPath,
		StateRoot:             root.Hex(),
		ThresholdAtto:         threshold.String(),
		OutputPath:            *outputPath,
		OutputSHA256:          hex.EncodeToString(outputHash.Sum(nil)),
		Accounts:              accounts,
		PositiveAccounts:      positiveAccounts,
		MissingPreimages:      missingPreimages,
		QualifyingAccounts:    qualifyingAccounts,
		QualifyingCodeLess:    qualifyingCodeLess,
		QualifyingCodeBearing: qualifyingCodeBearing,
		TotalBalanceAtto:      totalBalance.String(),
		QualifyingBalanceAtto: qualifyingBalance.String(),
		ElapsedMilliseconds:   elapsed.Milliseconds(),
		AccountsPerSecond:     int64(float64(accounts) / elapsed.Seconds()),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
	if *requirePreimages && missingPreimages != 0 {
		os.Exit(3)
	}
}

type outputHashWriter struct {
	output *bufio.Writer
	hash   interface{ Write([]byte) (int, error) }
}

func (w outputHashWriter) Write(p []byte) (int, error) {
	if _, err := w.hash.Write(p); err != nil {
		return 0, err
	}
	return w.output.Write(p)
}
