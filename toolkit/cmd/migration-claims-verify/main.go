package main

import (
	"bufio"
	"bytes"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/big"
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

const (
	priceNumerator = int64(74801)
	attoDecimals   = 18
	usdDecimals    = 26
)

var componentNames = []string{
	"liquid_shard0",
	"liquid_shard1",
	"liquid_total",
	"active_staked_or_delegated",
	"pending_undelegation",
	"unclaimed_staking_reward",
	"pending_cross_shard",
	"wallet_airdrop",
	"staked_to_vault",
	"total_claim",
}

type totals struct {
	Rows                        uint64 `json:"rows"`
	RowsWithAddress             uint64 `json:"rows_with_address"`
	RowsWithoutAddress          uint64 `json:"rows_without_address"`
	StrictOverOneUSD            uint64 `json:"strict_over_one_usd_rows"`
	LiquidShard0Atto            string `json:"liquid_shard0_atto"`
	LiquidShard1Atto            string `json:"liquid_shard1_atto"`
	LiquidTotalAtto             string `json:"liquid_total_atto"`
	ActiveStakedOrDelegatedAtto string `json:"active_staked_or_delegated_atto"`
	PendingUndelegationAtto     string `json:"pending_undelegation_atto"`
	UnclaimedStakingRewardAtto  string `json:"unclaimed_staking_reward_atto"`
	PendingCrossShardAtto       string `json:"pending_cross_shard_atto"`
	WalletAirdropAtto           string `json:"wallet_airdrop_atto"`
	StakedToVaultAtto           string `json:"staked_to_vault_atto"`
	TotalClaimAtto              string `json:"total_claim_atto"`
	StrictOverOneUSDTotalClaim  string `json:"strict_over_one_usd_total_claim_atto"`
	OriginalLiquidTotalAtto     string `json:"original_liquid_total_atto,omitempty"`
}

type accumulators struct {
	rows, rowsWithAddress, rowsWithoutAddress, strictOverOneUSD uint64
	components                                                  map[string]*big.Int
	overOneTotalClaim, originalLiquidTotal                      *big.Int
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func columnIndexes(header []string) map[string]int {
	indexes := make(map[string]int, len(header))
	for index, name := range header {
		if _, duplicate := indexes[name]; duplicate {
			fatalf("duplicate CSV column %q", name)
		}
		indexes[name] = index
	}
	return indexes
}

func field(row []string, columns map[string]int, name string, line uint64) string {
	index, ok := columns[name]
	if !ok {
		fatalf("missing CSV column %q", name)
	}
	if index >= len(row) {
		fatalf("row %d has no value for %q", line, name)
	}
	return row[index]
}

func unsigned(value, name string, line uint64) *big.Int {
	number, ok := new(big.Int).SetString(value, 10)
	if !ok || number.Sign() < 0 {
		fatalf("row %d has invalid %s %q", line, name, value)
	}
	return number
}

func fixedDecimal(value string, scale int, name string, line uint64) *big.Int {
	parts := strings.Split(value, ".")
	if len(parts) != 2 || len(parts[1]) != scale {
		fatalf("row %d has invalid fixed decimal %s %q", line, name, value)
	}
	return unsigned(parts[0]+parts[1], name, line)
}

func verifyAddress(key common.Hash, address string, line uint64) {
	if !common.IsHexAddress(address) {
		fatalf("row %d has invalid address %q", line, address)
	}
	if crypto.Keccak256Hash(common.HexToAddress(address).Bytes()) != key {
		fatalf("row %d address %s does not match secure key %s", line, address, key)
	}
}

func main() {
	var (
		input                  = flag.String("input", "", "migration entitlement CSV")
		requireOverOneUSD      = flag.Bool("require-over-one-usd", false, "require every total claim to exceed $1")
		requireOriginalOverUSD = flag.Bool("require-original-liquid-over-one-usd", false, "require every original liquid value to exceed $1")
		requireAllAddresses    = flag.Bool("require-all-addresses", false, "require every secure key to have a verified address")
		expectedShard0Block    = flag.String("expected-shard0-block", "93529887", "required claims shard-0 block")
		expectedShard1Block    = flag.String("expected-shard1-block", "94978278", "required claims shard-1 block")
		expectedPriceBlock     = flag.String("expected-price-reference-shard0-block", "93448483", "required price-reference block")
		expectedPrice          = flag.String("expected-price-usd-per-one", "0.00074801", "required USD price")
	)
	flag.Parse()
	if *input == "" {
		flag.Usage()
		os.Exit(2)
	}

	file, err := os.Open(*input)
	if err != nil {
		fatalf("open input: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 4*1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read header: %v", err)
	}
	columns := columnIndexes(header)

	acc := accumulators{
		components:          make(map[string]*big.Int, len(componentNames)),
		overOneTotalClaim:   new(big.Int),
		originalLiquidTotal: new(big.Int),
	}
	for _, component := range componentNames {
		acc.components[component] = new(big.Int)
	}
	threshold := new(big.Int).Exp(big.NewInt(10), big.NewInt(usdDecimals), nil)
	price := big.NewInt(priceNumerator)
	var previous common.Hash
	var havePrevious bool

	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read row %d: %v", acc.rows+2, err)
		}
		line := acc.rows + 2
		if len(row) != len(header) {
			fatalf("row %d has %d fields, expected %d", line, len(row), len(header))
		}
		keyText := field(row, columns, "secure_key", line)
		keyBytes, err := hex.DecodeString(strings.TrimPrefix(keyText, "0x"))
		if err != nil || len(keyBytes) != common.HashLength {
			fatalf("row %d has invalid secure key %q", line, keyText)
		}
		key := common.BytesToHash(keyBytes)
		if havePrevious && bytes.Compare(previous[:], key[:]) >= 0 {
			fatalf("row %d secure keys are not strictly increasing", line)
		}
		previous, havePrevious = key, true

		address := field(row, columns, "address", line)
		addressOrKey := field(row, columns, "address_or_secure_key", line)
		resolved := field(row, columns, "address_resolved", line)
		if address == "" {
			if resolved != "false" || !strings.EqualFold(addressOrKey, keyText) {
				fatalf("row %d has inconsistent unresolved-address fields", line)
			}
			acc.rowsWithoutAddress++
		} else {
			verifyAddress(key, address, line)
			if resolved != "true" || !strings.EqualFold(addressOrKey, address) {
				fatalf("row %d has inconsistent resolved-address fields", line)
			}
			acc.rowsWithAddress++
		}
		if field(row, columns, "claims_shard0_block", line) != *expectedShard0Block ||
			field(row, columns, "claims_shard1_block", line) != *expectedShard1Block ||
			field(row, columns, "valuation_price_reference_shard0_block", line) != *expectedPriceBlock ||
			field(row, columns, "valuation_price_usd_per_one", line) != *expectedPrice {
			fatalf("row %d has unexpected block or valuation metadata", line)
		}

		values := make(map[string]*big.Int, len(componentNames))
		for _, component := range componentNames {
			attoName := component + "_atto"
			oneName := component + "_one"
			value := unsigned(field(row, columns, attoName, line), attoName, line)
			one := fixedDecimal(field(row, columns, oneName, line), attoDecimals, oneName, line)
			if value.Cmp(one) != 0 {
				fatalf("row %d %s does not reproduce %s", line, oneName, attoName)
			}
			values[component] = value
			acc.components[component].Add(acc.components[component], value)
		}
		expectedLiquid := new(big.Int).Add(values["liquid_shard0"], values["liquid_shard1"])
		if expectedLiquid.Cmp(values["liquid_total"]) != 0 {
			fatalf("row %d liquid total mismatch", line)
		}
		expectedWalletAirdrop := new(big.Int)
		for _, component := range []string{
			"liquid_shard0",
			"liquid_shard1",
			"pending_undelegation",
			"unclaimed_staking_reward",
			"pending_cross_shard",
		} {
			expectedWalletAirdrop.Add(expectedWalletAirdrop, values[component])
		}
		if expectedWalletAirdrop.Cmp(values["wallet_airdrop"]) != 0 {
			fatalf("row %d wallet airdrop mismatch", line)
		}
		if values["active_staked_or_delegated"].Cmp(values["staked_to_vault"]) != 0 {
			fatalf("row %d staked-to-vault mismatch", line)
		}
		expectedTotalClaim := new(big.Int).Add(
			values["wallet_airdrop"],
			values["staked_to_vault"],
		)
		if expectedTotalClaim.Cmp(values["total_claim"]) != 0 {
			fatalf("row %d total claim mismatch", line)
		}
		expectedUSD := new(big.Int).Mul(values["total_claim"], price)
		gotTotalUSD := fixedDecimal(
			field(row, columns, "total_usd", line),
			usdDecimals,
			"total_usd",
			line,
		)
		if expectedUSD.Cmp(gotTotalUSD) != 0 {
			fatalf("row %d total USD mismatch", line)
		}
		expectedWalletUSD := new(big.Int).Mul(values["wallet_airdrop"], price)
		gotWalletUSD := fixedDecimal(
			field(row, columns, "wallet_airdrop_usd", line),
			usdDecimals,
			"wallet_airdrop_usd",
			line,
		)
		if expectedWalletUSD.Cmp(gotWalletUSD) != 0 {
			fatalf("row %d wallet-airdrop USD mismatch", line)
		}
		overOne := expectedUSD.Cmp(threshold) > 0
		if overOne {
			acc.strictOverOneUSD++
			acc.overOneTotalClaim.Add(acc.overOneTotalClaim, values["total_claim"])
		}
		if *requireOverOneUSD && !overOne {
			fatalf("row %d does not strictly exceed $1", line)
		}

		if *requireOriginalOverUSD {
			original := unsigned(
				field(row, columns, "original_liquid_total_atto", line),
				"original_liquid_total_atto",
				line,
			)
			original0 := unsigned(
				field(row, columns, "original_liquid_shard0_atto", line),
				"original_liquid_shard0_atto",
				line,
			)
			original1 := unsigned(
				field(row, columns, "original_liquid_shard1_atto", line),
				"original_liquid_shard1_atto",
				line,
			)
			if new(big.Int).Add(original0, original1).Cmp(original) != 0 {
				fatalf("row %d original liquid total mismatch", line)
			}
			if new(big.Int).Mul(original, price).Cmp(threshold) <= 0 {
				fatalf("row %d original liquid does not strictly exceed $1", line)
			}
			acc.originalLiquidTotal.Add(acc.originalLiquidTotal, original)
		}
		acc.rows++
	}
	if *requireAllAddresses && acc.rowsWithoutAddress != 0 {
		fatalf("%d rows do not have a verified address", acc.rowsWithoutAddress)
	}

	result := totals{
		Rows:                        acc.rows,
		RowsWithAddress:             acc.rowsWithAddress,
		RowsWithoutAddress:          acc.rowsWithoutAddress,
		StrictOverOneUSD:            acc.strictOverOneUSD,
		LiquidShard0Atto:            acc.components["liquid_shard0"].String(),
		LiquidShard1Atto:            acc.components["liquid_shard1"].String(),
		LiquidTotalAtto:             acc.components["liquid_total"].String(),
		ActiveStakedOrDelegatedAtto: acc.components["active_staked_or_delegated"].String(),
		PendingUndelegationAtto:     acc.components["pending_undelegation"].String(),
		UnclaimedStakingRewardAtto:  acc.components["unclaimed_staking_reward"].String(),
		PendingCrossShardAtto:       acc.components["pending_cross_shard"].String(),
		WalletAirdropAtto:           acc.components["wallet_airdrop"].String(),
		StakedToVaultAtto:           acc.components["staked_to_vault"].String(),
		TotalClaimAtto:              acc.components["total_claim"].String(),
		StrictOverOneUSDTotalClaim:  acc.overOneTotalClaim.String(),
	}
	if *requireOriginalOverUSD {
		result.OriginalLiquidTotalAtto = acc.originalLiquidTotal.String()
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}
