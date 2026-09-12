package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
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
	DBPath              string `json:"db_path"`
	OldRoot             string `json:"old_root"`
	NewRoot             string `json:"new_root"`
	OutputPath          string `json:"output_path"`
	OutputSHA256        string `json:"output_sha256"`
	CreatedAccounts     uint64 `json:"created_accounts"`
	ChangedAccounts     uint64 `json:"changed_accounts"`
	DeletedAccounts     uint64 `json:"deleted_accounts"`
	MissingPreimages    uint64 `json:"missing_preimages"`
	PositiveDeltaAtto   string `json:"positive_delta_atto"`
	NegativeDeltaAtto   string `json:"negative_delta_atto"`
	NetLiquidDeltaAtto  string `json:"net_liquid_delta_atto"`
	ForwardNodesScanned int    `json:"forward_nodes_scanned"`
	ReverseNodesScanned int    `json:"reverse_nodes_scanned"`
	ElapsedMilliseconds int64  `json:"elapsed_milliseconds"`
}

type accountValue struct {
	balance  *big.Int
	nonce    uint64
	codeHash []byte
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) common.Hash {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid 32-byte root %q", value)
	}
	return common.BytesToHash(decoded)
}

func decodeAccount(encoded []byte, key []byte) accountValue {
	if len(encoded) == 0 {
		return accountValue{
			balance:  new(big.Int),
			codeHash: types.EmptyCodeHash.Bytes(),
		}
	}
	var account types.StateAccount
	if err := rlp.DecodeBytes(encoded, &account); err != nil {
		fatalf("decode account %x: %v", key, err)
	}
	if account.Balance == nil || account.Balance.Sign() < 0 {
		fatalf("invalid account balance %x", key)
	}
	return accountValue{
		balance:  new(big.Int).Set(account.Balance),
		nonce:    account.Nonce,
		codeHash: common.CopyBytes(account.CodeHash),
	}
}

func main() {
	var (
		dbPath  = flag.String("db", "", "path to archive LevelDB")
		oldRoot = flag.String("old-root", "", "old account state root")
		newRoot = flag.String("new-root", "", "new account state root")
		output  = flag.String("output", "", "CSV output path")
		cacheMB = flag.Int("cache-mb", 4096, "LevelDB/trie cache in MiB")
		handles = flag.Int("handles", 4096, "LevelDB open-file handles")
	)
	flag.Parse()
	if *dbPath == "" || *oldRoot == "" || *newRoot == "" || *output == "" {
		flag.Usage()
		os.Exit(2)
	}
	oldRootHash := parseHash(*oldRoot)
	newRootHash := parseHash(*newRoot)

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()
	trieDB := trie.NewDatabase(db)
	oldTrie, err := trie.New(trie.StateTrieID(oldRootHash), trieDB)
	if err != nil {
		fatalf("open old trie: %v", err)
	}
	newTrie, err := trie.New(trie.StateTrieID(newRootHash), trieDB)
	if err != nil {
		fatalf("open new trie: %v", err)
	}
	oldLookupTrie, err := trie.New(trie.StateTrieID(oldRootHash), trieDB)
	if err != nil {
		fatalf("open old lookup trie: %v", err)
	}
	newLookupTrie, err := trie.New(trie.StateTrieID(newRootHash), trieDB)
	if err != nil {
		fatalf("open new lookup trie: %v", err)
	}

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
		"secure_key",
		"address",
		"change_type",
		"old_balance_atto",
		"new_balance_atto",
		"balance_delta_atto",
		"old_nonce",
		"new_nonce",
		"old_code_hash",
		"new_code_hash",
	}); err != nil {
		fatalf("write CSV header: %v", err)
	}

	started := time.Now()
	positiveDelta := new(big.Int)
	negativeDelta := new(big.Int)
	netDelta := new(big.Int)
	var created, changed, deleted, missingPreimages uint64

	writeChange := func(key []byte, oldValue, newValue accountValue, changeType string) {
		delta := new(big.Int).Sub(newValue.balance, oldValue.balance)
		if delta.Sign() > 0 {
			positiveDelta.Add(positiveDelta, delta)
		} else if delta.Sign() < 0 {
			negativeDelta.Add(negativeDelta, new(big.Int).Neg(delta))
		}
		netDelta.Add(netDelta, delta)

		address := ""
		preimage := rawdb.ReadPreimage(db, common.BytesToHash(key))
		if len(preimage) == 0 {
			missingPreimages++
		} else {
			if len(preimage) != common.AddressLength ||
				crypto.Keccak256Hash(preimage) != common.BytesToHash(key) {
				fatalf("invalid preimage for account %x", key)
			}
			address = common.BytesToAddress(preimage).Hex()
		}
		if err := writer.Write([]string{
			"0x" + hex.EncodeToString(key),
			address,
			changeType,
			oldValue.balance.String(),
			newValue.balance.String(),
			delta.String(),
			fmt.Sprintf("%d", oldValue.nonce),
			fmt.Sprintf("%d", newValue.nonce),
			"0x" + hex.EncodeToString(oldValue.codeHash),
			"0x" + hex.EncodeToString(newValue.codeHash),
		}); err != nil {
			fatalf("write account change: %v", err)
		}
	}

	forward, forwardCount := trie.NewDifferenceIterator(
		oldTrie.NodeIterator(nil),
		newTrie.NodeIterator(nil),
	)
	forwardLeaves := trie.NewIterator(forward)
	for forwardLeaves.Next() {
		oldEncoded, err := oldLookupTrie.TryGet(forwardLeaves.Key)
		if err != nil {
			fatalf("read old account %x: %v", forwardLeaves.Key, err)
		}
		oldValue := decodeAccount(oldEncoded, forwardLeaves.Key)
		newValue := decodeAccount(forwardLeaves.Value, forwardLeaves.Key)
		if len(oldEncoded) == 0 {
			created++
			writeChange(forwardLeaves.Key, oldValue, newValue, "created")
		} else {
			changed++
			writeChange(forwardLeaves.Key, oldValue, newValue, "changed")
		}
	}
	if forwardLeaves.Err != nil {
		fatalf("forward difference iteration: %v", forwardLeaves.Err)
	}

	reverse, reverseCount := trie.NewDifferenceIterator(
		newTrie.NodeIterator(nil),
		oldTrie.NodeIterator(nil),
	)
	reverseLeaves := trie.NewIterator(reverse)
	for reverseLeaves.Next() {
		newEncoded, err := newLookupTrie.TryGet(reverseLeaves.Key)
		if err != nil {
			fatalf("read new account %x: %v", reverseLeaves.Key, err)
		}
		if len(newEncoded) != 0 {
			continue
		}
		deleted++
		writeChange(
			reverseLeaves.Key,
			decodeAccount(reverseLeaves.Value, reverseLeaves.Key),
			decodeAccount(nil, reverseLeaves.Key),
			"deleted",
		)
	}
	if reverseLeaves.Err != nil {
		fatalf("reverse difference iteration: %v", reverseLeaves.Err)
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
		DBPath:              *dbPath,
		OldRoot:             oldRootHash.Hex(),
		NewRoot:             newRootHash.Hex(),
		OutputPath:          *output,
		OutputSHA256:        hex.EncodeToString(hasher.Sum(nil)),
		CreatedAccounts:     created,
		ChangedAccounts:     changed,
		DeletedAccounts:     deleted,
		MissingPreimages:    missingPreimages,
		PositiveDeltaAtto:   positiveDelta.String(),
		NegativeDeltaAtto:   negativeDelta.String(),
		NetLiquidDeltaAtto:  netDelta.String(),
		ForwardNodesScanned: *forwardCount,
		ReverseNodesScanned: *reverseCount,
		ElapsedMilliseconds: time.Since(started).Milliseconds(),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}
