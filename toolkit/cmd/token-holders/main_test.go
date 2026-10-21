package main

import (
	"context"
	"encoding/gob"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/common"
)

func word(address common.Address) string {
	return fmt.Sprintf("%064x", address.Bytes())
}

func amount(value int64) string {
	return fmt.Sprintf("%064x", value)
}

func TestApplyLogReplaysBothTransferLayouts(t *testing.T) {
	token := common.HexToAddress("0x03ff0ff224f904be3118461335064bb48df47938")
	alice := common.HexToAddress("0x0000000000000000000000000000000000000001")
	bob := common.HexToAddress("0x0000000000000000000000000000000000000002")
	logs := []rpcLog{
		{
			Address: token.Hex(),
			Topics:  []string{transferTopic, "0x" + word(common.Address{}), "0x" + word(alice)},
			Data:    "0x" + amount(100),
		},
		{
			Address: token.Hex(),
			Topics:  []string{transferTopic},
			Data:    "0x" + word(alice) + word(bob) + amount(30),
		},
		{
			Address: token.Hex(),
			Topics:  []string{transferTopic, "0x" + word(bob), "0x" + word(bob)},
			Data:    "0x" + amount(5),
		},
		{
			Address: token.Hex(),
			Topics:  []string{transferTopic, "0x" + word(alice), "0x" + word(common.Address{})},
			Data:    "0x" + amount(20),
		},
	}
	sets := make(balanceSet)
	for _, log := range logs {
		if err := applyLog(log, map[common.Address]bool{token: true}, sets, nil); err != nil {
			t.Fatal(err)
		}
	}
	balances := sets[token]
	if got := balances[alice].String(); got != "50" {
		t.Fatalf("alice = %s, want 50", got)
	}
	if got := balances[bob].String(); got != "30" {
		t.Fatalf("bob = %s, want 30", got)
	}
	if _, ok := balances[common.Address{}]; ok {
		t.Fatal("zero address must not hold a balance")
	}
}

func TestApplyLogRejectsForeignAndRemovedLogs(t *testing.T) {
	token := common.HexToAddress("0x03ff0ff224f904be3118461335064bb48df47938")
	other := common.HexToAddress("0xd5cd84d6f044abe314ee7e414d37cae8773ef9d3")
	base := rpcLog{
		Address: other.Hex(),
		Topics:  []string{transferTopic, "0x" + word(token), "0x" + word(token)},
		Data:    "0x" + amount(1),
	}
	tokens := map[common.Address]bool{token: true}
	if err := applyLog(base, tokens, balanceSet{}, nil); err == nil {
		t.Fatal("accepted a log from another contract")
	}
	base.Address = token.Hex()
	base.Removed = true
	if err := applyLog(base, tokens, balanceSet{}, nil); err == nil {
		t.Fatal("accepted a removed log")
	}
}

func TestApplyLogRoutesEachTokenSeparately(t *testing.T) {
	first := common.HexToAddress("0x03ff0ff224f904be3118461335064bb48df47938")
	second := common.HexToAddress("0xd5cd84d6f044abe314ee7e414d37cae8773ef9d3")
	alice := common.HexToAddress("0x0000000000000000000000000000000000000001")
	tokens := map[common.Address]bool{first: true, second: true}
	sets := make(balanceSet)
	for token, value := range map[common.Address]int64{first: 7, second: 11} {
		log := rpcLog{
			Address: token.Hex(),
			Topics:  []string{transferTopic, "0x" + word(common.Address{}), "0x" + word(alice)},
			Data:    "0x" + amount(value),
		}
		if err := applyLog(log, tokens, sets, nil); err != nil {
			t.Fatal(err)
		}
	}
	if sets[first][alice].Int64() != 7 || sets[second][alice].Int64() != 11 {
		t.Fatalf("balances were not kept per token: %v %v", sets[first][alice], sets[second][alice])
	}
}

func TestSampleAddressesTakesLargestFirstWithoutDuplicates(t *testing.T) {
	var addresses []common.Address
	balances := make(map[common.Address]*big.Int)
	for index := 1; index <= 10; index++ {
		address := common.BigToAddress(big.NewInt(int64(index)))
		addresses = append(addresses, address)
		balances[address] = big.NewInt(int64(index * 10))
	}
	sample := sampleAddresses(addresses, balances, 3, 4)
	if len(sample) != 7 {
		t.Fatalf("sample size = %d, want 7", len(sample))
	}
	for index, want := range []int64{100, 90, 80} {
		if balances[sample[index]].Int64() != want {
			t.Fatalf("sample[%d] balance = %s, want %d", index, balances[sample[index]], want)
		}
	}
	seen := make(map[common.Address]bool)
	for _, address := range sample {
		if seen[address] {
			t.Fatalf("duplicate sample address %s", address.Hex())
		}
		seen[address] = true
	}
}

func TestApplyLogSkipsLogsAlreadySeen(t *testing.T) {
	token := common.HexToAddress("0x03ff0ff224f904be3118461335064bb48df47938")
	alice := common.HexToAddress("0x0000000000000000000000000000000000000001")
	log := rpcLog{
		Address:         token.Hex(),
		Topics:          []string{transferTopic, "0x" + word(common.Address{}), "0x" + word(alice)},
		Data:            "0x" + amount(5),
		TransactionHash: "0xf5d7ba7eed725b4ad61be9ddcba6d7bc7b8b24a78db5eb78087c99494a6ba62f",
		LogIndex:        "0x3",
	}
	sets, seen := make(balanceSet), make(seenSet)
	tokens := map[common.Address]bool{token: true}
	for range 2 {
		if err := applyLog(log, tokens, sets, seen); err != nil {
			t.Fatal(err)
		}
	}
	if got := sets[token][alice].Int64(); got != 5 {
		t.Fatalf("balance = %d, want 5 after a repeated log", got)
	}
}

func TestCachedScanReusesAMatchingCache(t *testing.T) {
	token := common.HexToAddress("0x03ff0ff224f904be3118461335064bb48df47938")
	alice := common.HexToAddress("0x0000000000000000000000000000000000000001")
	path := filepath.Join(t.TempDir(), "scan.gob")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	cache := scanCache{
		Tokens:   []string{strings.ToLower(token.Hex())},
		From:     10,
		Block:    20,
		Balances: balanceSet{token: {alice: big.NewInt(9)}},
		Seen:     seenSet{token: {42: true}},
	}
	if err := gob.NewEncoder(file).Encode(cache); err != nil {
		t.Fatal(err)
	}
	file.Close()
	balances, seen, err := cachedScan(context.Background(), nil, path, []common.Address{token}, 10, 20, 1, 1, &scanStats{})
	if err != nil {
		t.Fatal(err)
	}
	if balances[token][alice].Int64() != 9 || !seen[token][42] {
		t.Fatal("cache contents were not returned")
	}
	if _, _, err := cachedScan(context.Background(), nil, path, []common.Address{token}, 11, 20, 1, 1, &scanStats{}); err == nil {
		t.Fatal("accepted a cache for another block range")
	}
}
