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

func TestValidatorCodePropagatesDatabaseErrors(t *testing.T) {
	_, _, err := validatorCode(
		valueReader{hasErr: errors.New("closed")},
		common.HexToHash("0x01"),
	)
	if err == nil {
		t.Fatal("validator code read error was suppressed")
	}
}
