package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math/big"
	"os"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
)

type Decimal struct {
	*big.Int
}

type CommissionRates struct {
	Rate          Decimal
	MaxRate       Decimal
	MaxChangeRate Decimal
}

type Commission struct {
	CommissionRates
	UpdateHeight *big.Int
}

type Description struct {
	Name            string
	Identity        string
	Website         string
	SecurityContact string
	Details         string
}

type Validator struct {
	Address              common.Address
	SlotPubKeys          [][48]byte
	LastEpochInCommittee *big.Int
	MinSelfDelegation    *big.Int
	MaxTotalDelegation   *big.Int
	Status               byte
	Commission
	Description
	CreationHeight *big.Int
}

type Undelegation struct {
	Amount *big.Int
	Epoch  *big.Int
}

type Delegation struct {
	DelegatorAddress common.Address
	Amount           *big.Int
	Reward           *big.Int
	Undelegations    []Undelegation
}

type Counters struct {
	NumBlocksToSign *big.Int
	NumBlocksSigned *big.Int
}

type ValidatorWrapper struct {
	Validator
	Delegations []Delegation
	Counters
	BlockReward *big.Int
}

type totals struct {
	DBPath                           string `json:"db_path"`
	StateRoot                        string `json:"state_root"`
	AccountCount                     uint64 `json:"account_count"`
	PositiveAccountCount             uint64 `json:"positive_account_count"`
	CodeBearingAccountCount          uint64 `json:"code_bearing_account_count"`
	LiquidBalanceAtto                string `json:"liquid_balance_atto"`
	StakingScanned                   bool   `json:"staking_scanned"`
	ValidatorOnly                    bool   `json:"validator_only"`
	ValidatorDiscovery               string `json:"validator_discovery"`
	ValidatorCount                   uint64 `json:"validator_count"`
	ValidatorListCount               uint64 `json:"validator_list_count"`
	ValidatorNotInListCount          uint64 `json:"validator_not_in_list_count"`
	DelegationCount                  uint64 `json:"delegation_count"`
	UndelegationEntryCount           uint64 `json:"undelegation_entry_count"`
	ActiveDelegationAtto             string `json:"active_delegation_atto"`
	PendingUndelegationAtto          string `json:"pending_undelegation_atto"`
	UnclaimedDelegationRewardAtto    string `json:"unclaimed_delegation_reward_atto"`
	ValidatorLifetimeBlockRewardAtto string `json:"validator_lifetime_block_reward_atto"`
	AccountAndStakingClaimsAtto      string `json:"account_and_staking_claims_atto"`
	ElapsedMilliseconds              int64  `json:"elapsed_milliseconds"`
	AccountsPerSecond                int64  `json:"accounts_per_second"`
}

type accumulator struct {
	accountCount                 uint64
	positiveAccountCount         uint64
	codeBearingAccountCount      uint64
	validatorCount               uint64
	validatorNotInListCount      uint64
	delegationCount              uint64
	undelegationEntryCount       uint64
	liquidBalance                *big.Int
	activeDelegation             *big.Int
	pendingUndelegation          *big.Int
	unclaimedDelegationReward    *big.Int
	validatorLifetimeBlockReward *big.Int
	validators                   map[common.Address]struct{}
	validatorList                map[common.Address]struct{}
	validatorKeys                map[common.Hash]struct{}
}

func newAccumulator() *accumulator {
	return &accumulator{
		liquidBalance:                new(big.Int),
		activeDelegation:             new(big.Int),
		pendingUndelegation:          new(big.Int),
		unclaimedDelegationReward:    new(big.Int),
		validatorLifetimeBlockReward: new(big.Int),
		validators:                   make(map[common.Address]struct{}),
		validatorList:                make(map[common.Address]struct{}),
		validatorKeys:                make(map[common.Hash]struct{}),
	}
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) common.Hash {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid 32-byte state root %q", value)
	}
	return common.BytesToHash(decoded)
}

func requireNonNegative(value *big.Int, field string, validator, delegator common.Address) {
	if value == nil || value.Sign() < 0 {
		fatalf(
			"invalid %s for validator %s delegator %s",
			field,
			validator.Hex(),
			delegator.Hex(),
		)
	}
}

func readValidatorList(db ethdb.KeyValueReader) map[common.Address]struct{} {
	encoded, err := db.Get([]byte("validator-list"))
	if err != nil || len(encoded) == 0 {
		fatalf("read validator-list: %v", err)
	}
	var addresses []common.Address
	if err := rlp.DecodeBytes(encoded, &addresses); err != nil {
		fatalf("decode validator-list: %v", err)
	}
	result := make(map[common.Address]struct{}, len(addresses))
	for _, address := range addresses {
		if _, exists := result[address]; exists {
			fatalf("duplicate address %s in validator-list", address.Hex())
		}
		result[address] = struct{}{}
	}
	return result
}

func validatorCode(db ethdb.KeyValueReader, codeHash common.Hash) (code []byte, prefixed bool) {
	key := append(append([]byte{}, []byte("vc")...), codeHash.Bytes()...)
	if encoded, err := db.Get(key); err == nil && len(encoded) != 0 {
		return encoded, true
	}
	if encoded, err := db.Get(codeHash.Bytes()); err == nil && len(encoded) != 0 {
		return encoded, false
	}
	return nil, false
}

func (sum *accumulator) addValidator(
	secureKey common.Hash,
	accountCodeHash common.Hash,
	encoded []byte,
	prefixed bool,
) {
	if got := crypto.Keccak256Hash(encoded); got != accountCodeHash {
		if prefixed {
			fatalf(
				"validator code hash mismatch at account key %s: got %s want %s",
				secureKey.Hex(),
				got.Hex(),
				accountCodeHash.Hex(),
			)
		}
		return
	}

	var wrapper ValidatorWrapper
	if err := rlp.DecodeBytes(encoded, &wrapper); err != nil {
		if prefixed {
			fatalf("decode prefixed validator code %s: %v", accountCodeHash.Hex(), err)
		}
		return
	}
	if crypto.Keccak256Hash(wrapper.Address.Bytes()) != secureKey {
		if prefixed {
			fatalf(
				"validator wrapper address %s does not match account key %s",
				wrapper.Address.Hex(),
				secureKey.Hex(),
			)
		}
		return
	}
	if _, exists := sum.validators[wrapper.Address]; exists {
		fatalf("duplicate validator wrapper %s", wrapper.Address.Hex())
	}
	sum.validators[wrapper.Address] = struct{}{}
	sum.validatorCount++
	if _, listed := sum.validatorList[wrapper.Address]; !listed {
		sum.validatorNotInListCount++
	}

	if wrapper.BlockReward == nil || wrapper.BlockReward.Sign() < 0 {
		fatalf("invalid lifetime block reward for validator %s", wrapper.Address.Hex())
	}
	sum.validatorLifetimeBlockReward.Add(
		sum.validatorLifetimeBlockReward,
		wrapper.BlockReward,
	)

	for i := range wrapper.Delegations {
		delegation := &wrapper.Delegations[i]
		requireNonNegative(
			delegation.Amount,
			"delegation amount",
			wrapper.Address,
			delegation.DelegatorAddress,
		)
		requireNonNegative(
			delegation.Reward,
			"delegation reward",
			wrapper.Address,
			delegation.DelegatorAddress,
		)
		sum.delegationCount++
		sum.activeDelegation.Add(sum.activeDelegation, delegation.Amount)
		sum.unclaimedDelegationReward.Add(
			sum.unclaimedDelegationReward,
			delegation.Reward,
		)
		for j := range delegation.Undelegations {
			undelegation := &delegation.Undelegations[j]
			requireNonNegative(
				undelegation.Amount,
				"undelegation amount",
				wrapper.Address,
				delegation.DelegatorAddress,
			)
			if undelegation.Epoch == nil || undelegation.Epoch.Sign() < 0 {
				fatalf(
					"invalid undelegation epoch for validator %s delegator %s",
					wrapper.Address.Hex(),
					delegation.DelegatorAddress.Hex(),
				)
			}
			sum.undelegationEntryCount++
			sum.pendingUndelegation.Add(
				sum.pendingUndelegation,
				undelegation.Amount,
			)
		}
	}
}

func main() {
	var (
		dbPath             = flag.String("db", "", "path to a Harmony shard LevelDB")
		rootText           = flag.String("root", "", "state root hash")
		output             = flag.String("output", "", "JSON output path")
		cacheMB            = flag.Int("cache-mb", 512, "LevelDB/trie cache in MiB")
		handles            = flag.Int("handles", 512, "LevelDB open-file handles")
		staking            = flag.Bool("staking", false, "scan and require beacon-chain validator state")
		validatorOnly      = flag.Bool("validator-only", false, "scan validator state without traversing all accounts")
		discoverValidators = flag.Bool("discover-validators", false, "discover validators from the selected state root instead of current validator-list")
	)
	flag.Parse()
	if *dbPath == "" || *rootText == "" || *output == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *validatorOnly && !*staking {
		fatalf("-validator-only requires -staking")
	}
	if *discoverValidators && !*staking {
		fatalf("-discover-validators requires -staking")
	}
	if *validatorOnly && *discoverValidators {
		fatalf("-validator-only cannot be combined with -discover-validators")
	}
	root := parseHash(*rootText)

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()

	sum := newAccumulator()
	if *staking && !*discoverValidators {
		sum.validatorList = readValidatorList(db)
		for validatorAddress := range sum.validatorList {
			sum.validatorKeys[crypto.Keccak256Hash(validatorAddress.Bytes())] = struct{}{}
		}
	}
	trieDB := trie.NewDatabase(db)
	stateTrie, err := trie.NewStateTrie(trie.StateTrieID(root), trieDB)
	if err != nil {
		fatalf("open state trie %s: %v", root.Hex(), err)
	}
	if got := stateTrie.Hash(); got != root {
		fatalf("opened trie root mismatch: got %s want %s", got.Hex(), root.Hex())
	}
	if *staking && !*discoverValidators {
		for validatorAddress := range sum.validatorList {
			account, err := stateTrie.TryGetAccount(validatorAddress)
			if err != nil {
				fatalf("read validator account %s: %v", validatorAddress.Hex(), err)
			}
			if account == nil || bytes.Equal(account.CodeHash, types.EmptyCodeHash.Bytes()) {
				fatalf("validator account %s has no code", validatorAddress.Hex())
			}
			codeHash := common.BytesToHash(account.CodeHash)
			encoded, prefixed := validatorCode(db, codeHash)
			if len(encoded) == 0 {
				fatalf(
					"validator account %s has no validator code %s",
					validatorAddress.Hex(),
					codeHash.Hex(),
				)
			}
			sum.addValidator(
				crypto.Keccak256Hash(validatorAddress.Bytes()),
				codeHash,
				encoded,
				prefixed,
			)
		}
	}

	started := time.Now()
	if !*validatorOnly {
		lastProgress := started
		iter := trie.NewIterator(stateTrie.NodeIterator(nil))
		for iter.Next() {
			var account types.StateAccount
			if err := rlp.DecodeBytes(iter.Value, &account); err != nil {
				fatalf("decode account at secure key %x: %v", iter.Key, err)
			}
			if account.Balance == nil || account.Balance.Sign() < 0 {
				fatalf("invalid balance at secure key %x", iter.Key)
			}
			sum.accountCount++
			sum.liquidBalance.Add(sum.liquidBalance, account.Balance)
			if account.Balance.Sign() > 0 {
				sum.positiveAccountCount++
			}

			if !bytes.Equal(account.CodeHash, types.EmptyCodeHash.Bytes()) {
				sum.codeBearingAccountCount++
				if *staking {
					secureKey := common.BytesToHash(iter.Key)
					_, listed := sum.validatorKeys[secureKey]
					if *discoverValidators || !listed {
						codeHash := common.BytesToHash(account.CodeHash)
						if encoded, prefixed := validatorCode(db, codeHash); len(encoded) != 0 {
							sum.addValidator(
								secureKey,
								codeHash,
								encoded,
								prefixed,
							)
						}
					}
				}
			}
			if time.Since(lastProgress) >= 10*time.Second {
				elapsed := time.Since(started)
				fmt.Fprintf(
					os.Stderr,
					"progress accounts=%d validators=%d rate=%d/s key=%x\n",
					sum.accountCount,
					sum.validatorCount,
					int64(float64(sum.accountCount)/elapsed.Seconds()),
					iter.Key,
				)
				lastProgress = time.Now()
			}
		}
		if iter.Err != nil {
			fatalf("state trie iteration: %v", iter.Err)
		}
	}
	if *staking && !*discoverValidators {
		for validator := range sum.validatorList {
			if _, exists := sum.validators[validator]; !exists {
				fatalf("validator-list address %s has no wrapper in state", validator.Hex())
			}
		}
	}

	accountAndStakingClaims := new(big.Int).Set(sum.liquidBalance)
	accountAndStakingClaims.Add(accountAndStakingClaims, sum.activeDelegation)
	accountAndStakingClaims.Add(accountAndStakingClaims, sum.pendingUndelegation)
	accountAndStakingClaims.Add(
		accountAndStakingClaims,
		sum.unclaimedDelegationReward,
	)
	elapsed := time.Since(started)
	validatorDiscovery := "disabled"
	if *staking {
		validatorDiscovery = "validator-list"
		if *discoverValidators {
			validatorDiscovery = "state-root"
		}
	}
	result := totals{
		DBPath:                           *dbPath,
		StateRoot:                        root.Hex(),
		AccountCount:                     sum.accountCount,
		PositiveAccountCount:             sum.positiveAccountCount,
		CodeBearingAccountCount:          sum.codeBearingAccountCount,
		LiquidBalanceAtto:                sum.liquidBalance.String(),
		StakingScanned:                   *staking,
		ValidatorOnly:                    *validatorOnly,
		ValidatorDiscovery:               validatorDiscovery,
		ValidatorCount:                   sum.validatorCount,
		ValidatorListCount:               uint64(len(sum.validatorList)),
		ValidatorNotInListCount:          sum.validatorNotInListCount,
		DelegationCount:                  sum.delegationCount,
		UndelegationEntryCount:           sum.undelegationEntryCount,
		ActiveDelegationAtto:             sum.activeDelegation.String(),
		PendingUndelegationAtto:          sum.pendingUndelegation.String(),
		UnclaimedDelegationRewardAtto:    sum.unclaimedDelegationReward.String(),
		ValidatorLifetimeBlockRewardAtto: sum.validatorLifetimeBlockReward.String(),
		AccountAndStakingClaimsAtto:      accountAndStakingClaims.String(),
		ElapsedMilliseconds:              elapsed.Milliseconds(),
		AccountsPerSecond:                int64(float64(sum.accountCount) / elapsed.Seconds()),
	}

	partial := *output + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		file.Close()
		os.Remove(partial)
		fatalf("encode output: %v", err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		os.Remove(partial)
		fatalf("sync output: %v", err)
	}
	if err := file.Close(); err != nil {
		os.Remove(partial)
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, *output); err != nil {
		os.Remove(partial)
		fatalf("publish output: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode stdout: %v", err)
	}
}
