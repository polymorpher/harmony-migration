package main

import (
	"fmt"
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
)

func eventAddressTopic(address common.Address) string {
	return fmt.Sprintf("0x%064x", address.Bytes())
}

func eventAmountData(value int64) string {
	return fmt.Sprintf("0x%064x", value)
}

func TestApplyLogReconstructsBalances(t *testing.T) {
	alice := common.HexToAddress("0x0000000000000000000000000000000000000001")
	bob := common.HexToAddress("0x0000000000000000000000000000000000000002")
	contract := common.HexToAddress(
		"0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",
	)
	logs := []rpcLog{
		{
			Address:     contract.Hex(),
			Topics:      []string{depositTopic, eventAddressTopic(alice)},
			Data:        eventAmountData(100),
			BlockNumber: "0x1",
		},
		{
			Address: contract.Hex(),
			Topics: []string{
				transferTopic,
				eventAddressTopic(alice),
				eventAddressTopic(bob),
			},
			Data:        eventAmountData(40),
			BlockNumber: "0x2",
		},
		{
			Address: contract.Hex(),
			Topics: []string{
				transferTopic,
				eventAddressTopic(bob),
				eventAddressTopic(bob),
			},
			Data:        eventAmountData(10),
			BlockNumber: "0x3",
		},
		{
			Address:     contract.Hex(),
			Topics:      []string{withdrawTopic, eventAddressTopic(alice)},
			Data:        eventAmountData(5),
			BlockNumber: "0x4",
		},
	}
	balances := make(map[common.Address]*big.Int)
	var counts eventCounts
	for _, log := range logs {
		if err := applyLog(log, contract, balances, &counts); err != nil {
			t.Fatal(err)
		}
	}
	if got := balances[alice].String(); got != "55" {
		t.Fatalf("Alice balance = %s, want 55", got)
	}
	if got := balances[bob].String(); got != "40" {
		t.Fatalf("Bob balance = %s, want 40", got)
	}
	if counts.Deposits != 1 ||
		counts.Withdrawals != 1 ||
		counts.Transfers != 2 {
		t.Fatalf("unexpected event counts: %+v", counts)
	}
}

func TestFormatToken(t *testing.T) {
	value, ok := new(big.Int).SetString("123000000000000000045", 10)
	if !ok {
		t.Fatal("parse fixture")
	}
	if got := formatToken(value); got != "123.000000000000000045" {
		t.Fatalf("formatToken() = %q", got)
	}
}
