package main

import (
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/harmony-one/harmony/core/rawdb"
)

func TestDecodeLookupBlock(t *testing.T) {
	entry := rawdb.TxLookupEntry{
		BlockHash:  common.HexToHash("0x1234"),
		BlockIndex: 123,
		Index:      4,
	}
	encoded, err := rlp.EncodeToBytes(entry)
	if err != nil {
		t.Fatal(err)
	}
	number, err := decodeLookupBlock(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if number != 123 {
		t.Fatalf("block = %d, want 123", number)
	}
	number, err = decodeLookupBlock([]byte{0x01, 0x00})
	if err != nil {
		t.Fatal(err)
	}
	if number != 256 {
		t.Fatalf("compact block = %d, want 256", number)
	}
}

func TestBlockBitset(t *testing.T) {
	bitset := make([]uint64, 3)
	if !setBlock(bitset, 65) {
		t.Fatal("first set did not report new block")
	}
	if setBlock(bitset, 65) {
		t.Fatal("duplicate set reported new block")
	}
	if !hasBlock(bitset, 65) || hasBlock(bitset, 64) {
		t.Fatal("bitset lookup mismatch")
	}
}
