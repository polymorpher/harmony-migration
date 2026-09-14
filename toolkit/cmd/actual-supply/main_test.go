package main

import (
	"errors"
	"testing"

	"github.com/ethereum/go-ethereum/common"
)

type valueReader struct {
	hasErr error
}

func (reader valueReader) Has([]byte) (bool, error) {
	return false, reader.hasErr
}

func (reader valueReader) Get([]byte) ([]byte, error) {
	return nil, errors.New("unexpected Get")
}

func TestEffectiveValidatorDiscovery(t *testing.T) {
	tests := []struct {
		name          string
		staking       bool
		validatorOnly bool
		requested     bool
		expected      bool
	}{
		{"staking defaults to discovery", true, false, true, true},
		{"explicit list comparison", true, false, false, false},
		{"liquid-only scan", false, false, true, false},
		{"validator-only list scan", true, true, true, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			actual := effectiveValidatorDiscovery(
				test.staking,
				test.validatorOnly,
				test.requested,
			)
			if actual != test.expected {
				t.Fatalf("got %v, want %v", actual, test.expected)
			}
		})
	}
}

func TestValidatorCodePropagatesDatabaseErrors(t *testing.T) {
	_, _, err := validatorCode(
		valueReader{hasErr: errors.New("closed")},
		common.HexToHash("0x01"),
	)
	if err == nil {
		t.Fatal("validator code read error was suppressed")
	}
}
