package main

import (
	"bufio"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	ethtypes "github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
	"github.com/harmony-one/harmony/core/rawdb"
)

type accountValue struct {
	exists  bool
	balance *big.Int
	nonce   uint64
}

type result struct {
	SecureKey       string `json:"secure_key"`
	Address         string `json:"address"`
	CurrentBlock    uint64 `json:"current_block"`
	CurrentNonce    uint64 `json:"current_nonce"`
	TransitionBlock uint64 `json:"transition_block"`
	PreviousNonce   uint64 `json:"previous_nonce"`
	TransitionNonce uint64 `json:"transition_nonce"`
	TransactionHash string `json:"transaction_hash"`
	TransactionKind string `json:"transaction_kind"`
	StateLookups    uint64 `json:"state_lookups"`
}

type resolver struct {
	db           ethdb.Database
	trieDB       *trie.Database
	rootCache    map[uint64]common.Hash
	stateLookups uint64
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) common.Hash {
	decoded, err := hex.DecodeString(strings.TrimPrefix(value, "0x"))
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid secure key %q", value)
	}
	return common.BytesToHash(decoded)
}

func (resolver *resolver) rootAt(blockNumber uint64) common.Hash {
	if root, ok := resolver.rootCache[blockNumber]; ok {
		return root
	}
	hash := rawdb.ReadCanonicalHash(resolver.db, blockNumber)
	if hash == (common.Hash{}) {
		fatalf("missing canonical hash at block %d", blockNumber)
	}
	header := rawdb.ReadHeader(resolver.db, hash, blockNumber)
	if header == nil {
		fatalf("missing canonical header at block %d", blockNumber)
	}
	root := header.Root()
	resolver.rootCache[blockNumber] = root
	return root
}

func (resolver *resolver) accountAt(blockNumber uint64, key common.Hash) accountValue {
	stateTrie, err := trie.New(trie.StateTrieID(resolver.rootAt(blockNumber)), resolver.trieDB)
	if err != nil {
		fatalf("open state trie at block %d: %v", blockNumber, err)
	}
	encoded, err := stateTrie.TryGet(key.Bytes())
	if err != nil {
		fatalf("read account %s at block %d: %v", key.Hex(), blockNumber, err)
	}
	resolver.stateLookups++
	if len(encoded) == 0 {
		return accountValue{balance: new(big.Int)}
	}
	var account ethtypes.StateAccount
	if err := rlp.DecodeBytes(encoded, &account); err != nil {
		fatalf("decode account %s at block %d: %v", key.Hex(), blockNumber, err)
	}
	if account.Balance == nil || account.Balance.Sign() < 0 {
		fatalf("invalid account %s at block %d", key.Hex(), blockNumber)
	}
	return accountValue{
		exists:  true,
		balance: new(big.Int).Set(account.Balance),
		nonce:   account.Nonce,
	}
}

func writeMatch(path string, key common.Hash, address common.Address, blockNumber uint64, txHash common.Hash, kind string) {
	file, err := os.OpenFile(path+".partial", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create match output: %v", err)
	}
	buffered := bufio.NewWriter(file)
	writer := csv.NewWriter(buffered)
	if err := writer.Write([]string{
		"secure_key",
		"address",
		"source",
		"block_number",
		"transaction_hash",
		"detail",
	}); err != nil {
		fatalf("write match header: %v", err)
	}
	if err := writer.Write([]string{
		key.Hex(),
		address.Hex(),
		"canonical_nonce_transition",
		fmt.Sprintf("%d", blockNumber),
		txHash.Hex(),
		"transaction_kind=" + kind,
	}); err != nil {
		fatalf("write match row: %v", err)
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush match output: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush match buffer: %v", err)
	}
	if err := file.Sync(); err != nil {
		fatalf("sync match output: %v", err)
	}
	if err := file.Close(); err != nil {
		fatalf("close match output: %v", err)
	}
	if err := os.Rename(path+".partial", path); err != nil {
		fatalf("publish match output: %v", err)
	}
}

func main() {
	var (
		dbPath       = flag.String("db", "", "canonical archive LevelDB")
		secureKey    = flag.String("secure-key", "", "target account secure key")
		currentBlock = flag.Uint64("current-block", 0, "current state block")
		outputPath   = flag.String("output", "", "verified one-row match CSV")
		summaryPath  = flag.String("summary", "", "summary JSON")
		cacheMB      = flag.Int("cache-mb", 4096, "LevelDB cache in MiB")
		handles      = flag.Int("handles", 4096, "LevelDB open-file handles")
	)
	flag.Parse()
	if *dbPath == "" || *secureKey == "" || *currentBlock == 0 || *outputPath == "" || *summaryPath == "" {
		flag.Usage()
		os.Exit(2)
	}
	key := parseHash(*secureKey)

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open archive database: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	resolver := &resolver{
		db:        db,
		trieDB:    trie.NewDatabase(db),
		rootCache: make(map[uint64]common.Hash),
	}

	current := resolver.accountAt(*currentBlock, key)
	if !current.exists || current.nonce == 0 {
		db.Close()
		fatalf("current account %s does not have a positive nonce", key.Hex())
	}
	low, high := uint64(0), *currentBlock
	for low < high {
		middle := low + (high-low)/2
		if resolver.accountAt(middle, key).nonce >= current.nonce {
			high = middle
		} else {
			low = middle + 1
		}
	}
	transitionBlock := low
	transition := resolver.accountAt(transitionBlock, key)
	if transition.nonce < current.nonce {
		db.Close()
		fatalf("nonce transition upper bound is invalid")
	}
	previous := accountValue{balance: new(big.Int)}
	if transitionBlock > 0 {
		previous = resolver.accountAt(transitionBlock-1, key)
		if previous.nonce >= current.nonce {
			db.Close()
			fatalf("nonce transition lower bound is invalid")
		}
	}

	blockHash := rawdb.ReadCanonicalHash(db, transitionBlock)
	body := rawdb.ReadBody(db, blockHash, transitionBlock)
	if body == nil {
		db.Close()
		fatalf("missing canonical block body at nonce transition %d", transitionBlock)
	}
	var address common.Address
	var transactionHash common.Hash
	var transactionKind string
	for _, tx := range body.Transactions() {
		sender, err := tx.SenderAddress()
		if err != nil {
			continue
		}
		if crypto.Keccak256Hash(sender.Bytes()) == key {
			address = sender
			transactionHash = tx.HashByType()
			transactionKind = "regular"
			break
		}
	}
	if address == (common.Address{}) {
		for _, tx := range body.StakingTransactions() {
			sender, err := tx.SenderAddress()
			if err != nil {
				continue
			}
			if crypto.Keccak256Hash(sender.Bytes()) == key {
				address = sender
				transactionHash = tx.Hash()
				transactionKind = "staking"
				break
			}
		}
	}
	if address == (common.Address{}) {
		db.Close()
		fatalf("no signed transaction in block %d matches secure key %s", transitionBlock, key.Hex())
	}
	if crypto.Keccak256Hash(address.Bytes()) != key {
		db.Close()
		fatalf("internal address verification failed")
	}
	if err := db.Close(); err != nil {
		fatalf("close archive database: %v", err)
	}

	writeMatch(*outputPath, key, address, transitionBlock, transactionHash, transactionKind)
	output := result{
		SecureKey:       key.Hex(),
		Address:         address.Hex(),
		CurrentBlock:    *currentBlock,
		CurrentNonce:    current.nonce,
		TransitionBlock: transitionBlock,
		PreviousNonce:   previous.nonce,
		TransitionNonce: transition.nonce,
		TransactionHash: transactionHash.Hex(),
		TransactionKind: transactionKind,
		StateLookups:    resolver.stateLookups,
	}
	data, err := json.MarshalIndent(output, "", "  ")
	if err != nil {
		fatalf("encode summary: %v", err)
	}
	data = append(data, '\n')
	if err := os.WriteFile(*summaryPath+".partial", data, 0o600); err != nil {
		fatalf("write summary: %v", err)
	}
	if err := os.Rename(*summaryPath+".partial", *summaryPath); err != nil {
		fatalf("publish summary: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		fatalf("encode summary output: %v", err)
	}
}
