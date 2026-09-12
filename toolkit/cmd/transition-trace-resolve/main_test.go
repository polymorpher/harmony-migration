package main

import (
	"encoding/csv"
	"os"
	"path/filepath"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

func newTestResolver(t *testing.T, address common.Address) (*resolver, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "matches.csv")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = file.Close()
	})
	return &resolver{
		targets: map[common.Hash]struct{}{
			crypto.Keccak256Hash(address.Bytes()): {},
		},
		seen:   make(map[common.Address]struct{}),
		writer: csv.NewWriter(file),
		output: file,
	}, path
}

func readSingleMatch(t *testing.T, path string) []string {
	t.Helper()
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	rows, err := csv.NewReader(file).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	return rows[0]
}

func TestWalkMatchesDirectTraceAddress(t *testing.T) {
	address := common.HexToAddress("0x1234567890abcdef1234567890abcdef12345678")
	resolver, path := newTestResolver(t, address)
	trace := map[string]interface{}{
		"action": map[string]interface{}{
			"from": address.Hex(),
			"to":   "0x0000000000000000000000000000000000000001",
		},
	}

	resolver.walk(trace, 42, "0xtransaction", "trace[0]", "")
	resolver.writer.Flush()

	if len(resolver.targets) != 0 || resolver.matches != 1 {
		t.Fatalf("targets=%d matches=%d, want 0 and 1", len(resolver.targets), resolver.matches)
	}
	row := readSingleMatch(t, path)
	if row[1] != address.Hex() || row[3] != "42" || row[4] != "0xtransaction" {
		t.Fatalf("unexpected match row: %#v", row)
	}
}

func TestWalkMatchesABIEncodedAddress(t *testing.T) {
	address := common.HexToAddress("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
	resolver, path := newTestResolver(t, address)
	input := "0x12345678" +
		"000000000000000000000000" +
		"abcdefabcdefabcdefabcdefabcdefabcdefabcd"
	trace := map[string]interface{}{
		"action": map[string]interface{}{
			"input": input,
		},
	}

	resolver.walk(trace, 99, "", "trace[0]", "")
	resolver.writer.Flush()

	if len(resolver.targets) != 0 || resolver.matches != 1 {
		t.Fatalf("targets=%d matches=%d, want 0 and 1", len(resolver.targets), resolver.matches)
	}
	row := readSingleMatch(t, path)
	if row[1] != address.Hex() || row[3] != "99" {
		t.Fatalf("unexpected match row: %#v", row)
	}
}
