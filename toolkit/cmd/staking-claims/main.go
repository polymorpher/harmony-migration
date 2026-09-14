package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/big"
	"os"
	"sort"
	"strings"

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

type claim struct {
	secureKey           common.Hash
	address             common.Address
	active              *big.Int
	pendingUndelegation *big.Int
	reward              *big.Int
}

type activeDelegation struct {
	validator common.Address
	delegator common.Address
	amount    *big.Int
}

type summary struct {
	DBPath                  string `json:"db_path"`
	StateRoot               string `json:"state_root"`
	OutputPath              string `json:"output_path"`
	OutputSHA256            string `json:"output_sha256"`
	DelegationOutputPath    string `json:"delegation_output_path,omitempty"`
	DelegationOutputSHA256  string `json:"delegation_output_sha256,omitempty"`
	ValidatorDiscovery      string `json:"validator_discovery"`
	ValidatorCount          uint64 `json:"validator_count"`
	DelegationCount         uint64 `json:"delegation_count"`
	DelegatorCount          uint64 `json:"delegator_count"`
	UndelegationEntryCount  uint64 `json:"undelegation_entry_count"`
	ActiveDelegationRows    uint64 `json:"active_delegation_rows"`
	ActiveAtto              string `json:"active_atto"`
	PendingUndelegationAtto string `json:"pending_undelegation_atto"`
	RewardAtto              string `json:"reward_atto"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) common.Hash {
	value = strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(value)
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid state root %q", value)
	}
	return common.BytesToHash(decoded)
}

func readValidatorList(db ethdb.KeyValueReader) []common.Address {
	encoded, err := db.Get([]byte("validator-list"))
	if err != nil || len(encoded) == 0 {
		fatalf("read validator-list: %v", err)
	}
	var addresses []common.Address
	if err := rlp.DecodeBytes(encoded, &addresses); err != nil {
		fatalf("decode validator-list: %v", err)
	}
	seen := make(map[common.Address]struct{}, len(addresses))
	for _, address := range addresses {
		if _, duplicate := seen[address]; duplicate {
			fatalf("duplicate validator-list address %s", address.Hex())
		}
		seen[address] = struct{}{}
	}
	return addresses
}

func validatorCode(db ethdb.KeyValueReader, hash common.Hash) (code []byte, prefixed bool) {
	key := append([]byte("vc"), hash.Bytes()...)
	if encoded, err := db.Get(key); err == nil && len(encoded) != 0 {
		return encoded, true
	}
	if encoded, err := db.Get(hash.Bytes()); err == nil && len(encoded) != 0 {
		return encoded, false
	}
	return nil, false
}

func requireNonNegative(value *big.Int, name string, validator, delegator common.Address) {
	if value == nil || value.Sign() < 0 {
		fatalf(
			"invalid %s for validator %s delegator %s",
			name,
			validator.Hex(),
			delegator.Hex(),
		)
	}
}

func writeActiveDelegations(path string, rows []activeDelegation) string {
	if _, err := os.Stat(path); err == nil {
		fatalf("refusing to overwrite %s", path)
	} else if !os.IsNotExist(err) {
		fatalf("inspect delegation output %s: %v", path, err)
	}
	sort.Slice(rows, func(i, j int) bool {
		if comparison := bytesCompare(
			rows[i].validator.Bytes(),
			rows[j].validator.Bytes(),
		); comparison != 0 {
			return comparison < 0
		}
		return bytesCompare(
			rows[i].delegator.Bytes(),
			rows[j].delegator.Bytes(),
		) < 0
	})
	partial := path + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create delegation output: %v", err)
	}
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(
		io.MultiWriter(file, hasher),
		1024*1024,
	)
	writer := csv.NewWriter(buffered)
	if err := writer.Write([]string{
		"validator_address",
		"validator_secure_key",
		"delegator_address",
		"delegator_secure_key",
		"staked_to_vault_atto",
		"is_self_delegation",
	}); err != nil {
		fatalf("write delegation header: %v", err)
	}
	for _, row := range rows {
		if err := writer.Write([]string{
			row.validator.Hex(),
			crypto.Keccak256Hash(row.validator.Bytes()).Hex(),
			row.delegator.Hex(),
			crypto.Keccak256Hash(row.delegator.Bytes()).Hex(),
			row.amount.String(),
			fmt.Sprint(row.validator == row.delegator),
		}); err != nil {
			fatalf("write active delegation: %v", err)
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush delegation CSV: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush delegation output: %v", err)
	}
	if err := file.Sync(); err != nil {
		fatalf("sync delegation output: %v", err)
	}
	if err := file.Close(); err != nil {
		fatalf("close delegation output: %v", err)
	}
	if err := os.Rename(partial, path); err != nil {
		fatalf("publish delegation output: %v", err)
	}
	return hex.EncodeToString(hasher.Sum(nil))
}

func main() {
	var (
		dbPath            = flag.String("db", "", "path to shard-0 LevelDB")
		rootText          = flag.String("root", "", "state root hash")
		output            = flag.String("output", "", "staking claims CSV output path")
		delegationsOutput = flag.String(
			"delegations-output",
			"",
			"optional per-validator active-delegation CSV output path",
		)
		cacheMB  = flag.Int("cache-mb", 128, "LevelDB/trie cache in MiB")
		handles  = flag.Int("handles", 128, "LevelDB open-file handles")
		discover = flag.Bool("discover-validators", true, "discover validators from the selected state root instead of the database validator-list")
	)
	flag.Parse()
	if *dbPath == "" || *rootText == "" || *output == "" {
		flag.Usage()
		os.Exit(2)
	}
	root := parseHash(*rootText)

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	db := rawdb.NewDatabase(disk)
	defer db.Close()
	stateTrie, err := trie.NewStateTrie(trie.StateTrieID(root), trie.NewDatabase(db))
	if err != nil {
		fatalf("open state trie: %v", err)
	}

	claims := make(map[common.Hash]*claim)
	activeDelegations := make([]activeDelegation, 0)
	activeTotal := new(big.Int)
	pendingTotal := new(big.Int)
	rewardTotal := new(big.Int)
	var validatorCount, delegationCount, undelegationCount uint64
	seenValidators := make(map[common.Address]struct{})
	processValidator := func(
		secureKey common.Hash,
		codeHash common.Hash,
		encoded []byte,
		prefixed bool,
	) bool {
		if len(encoded) == 0 || crypto.Keccak256Hash(encoded) != codeHash {
			if prefixed {
				fatalf("validator code missing or hash mismatch for secure key %s", secureKey.Hex())
			}
			return false
		}
		var wrapper ValidatorWrapper
		if err := rlp.DecodeBytes(encoded, &wrapper); err != nil {
			if prefixed {
				fatalf("decode validator at secure key %s: %v", secureKey.Hex(), err)
			}
			return false
		}
		if crypto.Keccak256Hash(wrapper.Address.Bytes()) != secureKey {
			if prefixed {
				fatalf("validator wrapper address %s does not match secure key %s", wrapper.Address.Hex(), secureKey.Hex())
			}
			return false
		}
		if _, duplicate := seenValidators[wrapper.Address]; duplicate {
			fatalf("duplicate validator wrapper %s", wrapper.Address.Hex())
		}
		seenValidators[wrapper.Address] = struct{}{}
		validatorCount++

		seenDelegators := make(map[common.Address]struct{}, len(wrapper.Delegations))
		for i := range wrapper.Delegations {
			delegation := &wrapper.Delegations[i]
			if _, duplicate := seenDelegators[delegation.DelegatorAddress]; duplicate {
				fatalf(
					"duplicate delegator %s within validator %s",
					delegation.DelegatorAddress.Hex(),
					wrapper.Address.Hex(),
				)
			}
			seenDelegators[delegation.DelegatorAddress] = struct{}{}
			requireNonNegative(
				delegation.Amount,
				"active delegation",
				wrapper.Address,
				delegation.DelegatorAddress,
			)
			requireNonNegative(
				delegation.Reward,
				"reward",
				wrapper.Address,
				delegation.DelegatorAddress,
			)
			delegationCount++
			if delegation.Amount.Sign() > 0 {
				activeDelegations = append(
					activeDelegations,
					activeDelegation{
						validator: wrapper.Address,
						delegator: delegation.DelegatorAddress,
						amount:    new(big.Int).Set(delegation.Amount),
					},
				)
			}

			secureKey := crypto.Keccak256Hash(delegation.DelegatorAddress.Bytes())
			entry := claims[secureKey]
			if entry == nil {
				entry = &claim{
					secureKey:           secureKey,
					address:             delegation.DelegatorAddress,
					active:              new(big.Int),
					pendingUndelegation: new(big.Int),
					reward:              new(big.Int),
				}
				claims[secureKey] = entry
			} else if entry.address != delegation.DelegatorAddress {
				fatalf("secure-key collision for delegator %s", delegation.DelegatorAddress.Hex())
			}
			entry.active.Add(entry.active, delegation.Amount)
			entry.reward.Add(entry.reward, delegation.Reward)
			activeTotal.Add(activeTotal, delegation.Amount)
			rewardTotal.Add(rewardTotal, delegation.Reward)
			for j := range delegation.Undelegations {
				undelegation := &delegation.Undelegations[j]
				requireNonNegative(
					undelegation.Amount,
					"pending undelegation",
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
				undelegationCount++
				entry.pendingUndelegation.Add(
					entry.pendingUndelegation,
					undelegation.Amount,
				)
				pendingTotal.Add(pendingTotal, undelegation.Amount)
			}
		}
		return true
	}

	if *discover {
		iter := trie.NewIterator(stateTrie.NodeIterator(nil))
		for iter.Next() {
			var account types.StateAccount
			if err := rlp.DecodeBytes(iter.Value, &account); err != nil {
				fatalf("decode account at secure key %x: %v", iter.Key, err)
			}
			codeHash := common.BytesToHash(account.CodeHash)
			if codeHash == types.EmptyCodeHash {
				continue
			}
			if encoded, prefixed := validatorCode(db, codeHash); len(encoded) != 0 {
				processValidator(common.BytesToHash(iter.Key), codeHash, encoded, prefixed)
			}
		}
		if iter.Err != nil {
			fatalf("iterate state root for validators: %v", iter.Err)
		}
	} else {
		validators := readValidatorList(db)
		for _, validatorAddress := range validators {
			account, err := stateTrie.TryGetAccount(validatorAddress)
			if err != nil || account == nil {
				fatalf("read validator account %s: %v", validatorAddress.Hex(), err)
			}
			codeHash := common.BytesToHash(account.CodeHash)
			if codeHash == types.EmptyCodeHash {
				fatalf("validator %s has empty code hash", validatorAddress.Hex())
			}
			encoded, prefixed := validatorCode(db, codeHash)
			if !processValidator(
				crypto.Keccak256Hash(validatorAddress.Bytes()),
				codeHash,
				encoded,
				prefixed,
			) {
				fatalf("validator %s has no valid wrapper", validatorAddress.Hex())
			}
		}
	}

	keys := make([]common.Hash, 0, len(claims))
	for key, entry := range claims {
		if entry.active.Sign() != 0 ||
			entry.pendingUndelegation.Sign() != 0 ||
			entry.reward.Sign() != 0 {
			keys = append(keys, key)
		}
	}
	sort.Slice(keys, func(i, j int) bool {
		return bytesCompare(keys[i][:], keys[j][:]) < 0
	})

	partial := *output + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(io.MultiWriter(file, hasher), 1024*1024)
	writer := csv.NewWriter(buffered)
	if err := writer.Write([]string{
		"secure_key",
		"address",
		"active_delegation_atto",
		"pending_undelegation_atto",
		"unclaimed_reward_atto",
	}); err != nil {
		fatalf("write header: %v", err)
	}
	for _, key := range keys {
		entry := claims[key]
		if err := writer.Write([]string{
			key.Hex(),
			entry.address.Hex(),
			entry.active.String(),
			entry.pendingUndelegation.String(),
			entry.reward.String(),
		}); err != nil {
			fatalf("write claim: %v", err)
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush output: %v", err)
	}
	if err := file.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := file.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, *output); err != nil {
		fatalf("publish output: %v", err)
	}

	delegationOutputHash := ""
	if *delegationsOutput != "" {
		detailTotal := new(big.Int)
		for _, delegation := range activeDelegations {
			detailTotal.Add(detailTotal, delegation.amount)
		}
		if detailTotal.Cmp(activeTotal) != 0 {
			fatalf(
				"active delegation detail total %s does not match aggregate %s",
				detailTotal,
				activeTotal,
			)
		}
		delegationOutputHash = writeActiveDelegations(
			*delegationsOutput,
			activeDelegations,
		)
	}

	result := summary{
		DBPath:                  *dbPath,
		StateRoot:               root.Hex(),
		OutputPath:              *output,
		OutputSHA256:            hex.EncodeToString(hasher.Sum(nil)),
		DelegationOutputPath:    *delegationsOutput,
		DelegationOutputSHA256:  delegationOutputHash,
		ValidatorDiscovery:      map[bool]string{false: "validator-list", true: "state-root"}[*discover],
		ValidatorCount:          validatorCount,
		DelegationCount:         delegationCount,
		DelegatorCount:          uint64(len(keys)),
		UndelegationEntryCount:  undelegationCount,
		ActiveDelegationRows:    uint64(len(activeDelegations)),
		ActiveAtto:              activeTotal.String(),
		PendingUndelegationAtto: pendingTotal.String(),
		RewardAtto:              rewardTotal.String(),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}

func bytesCompare(a, b []byte) int {
	for i := range a {
		if a[i] < b[i] {
			return -1
		}
		if a[i] > b[i] {
			return 1
		}
	}
	return 0
}
