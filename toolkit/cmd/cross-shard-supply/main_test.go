package main

import (
	"errors"
	"testing"

	"github.com/ethereum/go-ethereum/common"
)

type lookupReader struct {
	has      bool
	hasErr   error
	value    []byte
	valueErr error
}

func (reader lookupReader) Has([]byte) (bool, error) {
	return reader.has, reader.hasErr
}

func (reader lookupReader) Get([]byte) ([]byte, error) {
	return reader.value, reader.valueErr
}

func TestReadCXLookupDistinguishesMissingAndFailedReads(t *testing.T) {
	hash := common.HexToHash("0x01")
	if value, applied, err := readCXLookup(
		lookupReader{},
		hash,
	); err != nil || applied || value != nil {
		t.Fatalf("missing lookup = %x, %v, %v", value, applied, err)
	}
	if _, _, err := readCXLookup(
		lookupReader{hasErr: errors.New("closed")},
		hash,
	); err == nil {
		t.Fatal("presence read error was accepted")
	}
	if _, _, err := readCXLookup(
		lookupReader{has: true, valueErr: errors.New("I/O")},
		hash,
	); err == nil {
		t.Fatal("value read error was accepted")
	}
	if _, _, err := readCXLookup(
		lookupReader{has: true},
		hash,
	); err == nil {
		t.Fatal("empty present lookup was accepted")
	}
	expected := []byte{0xc0}
	value, applied, err := readCXLookup(
		lookupReader{has: true, value: expected},
		hash,
	)
	if err != nil || !applied || string(value) != string(expected) {
		t.Fatalf("existing lookup = %x, %v, %v", value, applied, err)
	}
}

func TestReadCanonicalHashDistinguishesMissingAndFailedReads(t *testing.T) {
	if hash, exists, err := readCanonicalHash(
		lookupReader{},
		7,
	); err != nil || exists || hash != (common.Hash{}) {
		t.Fatalf("missing hash = %s, %v, %v", hash, exists, err)
	}
	if _, _, err := readCanonicalHash(
		lookupReader{hasErr: errors.New("closed")},
		7,
	); err == nil {
		t.Fatal("canonical presence read error was accepted")
	}
	if _, _, err := readCanonicalHash(
		lookupReader{has: true, valueErr: errors.New("I/O")},
		7,
	); err == nil {
		t.Fatal("canonical value read error was accepted")
	}
	expected := common.HexToHash("0x1234")
	hash, exists, err := readCanonicalHash(
		lookupReader{has: true, value: expected.Bytes()},
		7,
	)
	if err != nil || !exists || hash != expected {
		t.Fatalf("canonical hash = %s, %v, %v", hash, exists, err)
	}
}
