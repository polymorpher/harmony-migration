// Command token-holders reconstructs every positive ERC-20 balance at a
// pinned block on any EVM chain by replaying Transfer events, then requires
// the holder total to equal totalSupply() at that block and spot-checks
// individual balances with balanceOf().
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/gob"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash/fnv"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

const (
	transferTopic   = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
	totalSupplyCall = "0x18160ddd"
	decimalsCall    = "0x313ce567"
	balanceOfPrefix = "0x70a08231"
)

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *rpcError) Error() string {
	return fmt.Sprintf("JSON-RPC error %d: %s", e.Code, e.Message)
}

type rpcResponse struct {
	Result json.RawMessage `json:"result"`
	Error  *rpcError       `json:"error"`
}

type rpcClient struct {
	url      string
	http     *http.Client
	nextID   atomic.Uint64
	attempts int
}

func newRPCClient(endpoint string, attempts int) *rpcClient {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = 64
	return &rpcClient{
		url:      endpoint,
		http:     &http.Client{Transport: transport, Timeout: 2 * time.Minute},
		attempts: attempts,
	}
}

// call returns JSON-RPC errors immediately and retries transport failures
// and non-200 responses with a growing delay.
func (c *rpcClient) call(ctx context.Context, method string, params any, result any) error {
	payload, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      c.nextID.Add(1),
		"method":  method,
		"params":  params,
	})
	if err != nil {
		return fmt.Errorf("encode %s: %w", method, err)
	}
	var lastError error
	for attempt := 0; attempt < c.attempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		request.Header.Set("Content-Type", "application/json")
		response, err := c.http.Do(request)
		if err != nil {
			lastError = err
			continue
		}
		var decoded rpcResponse
		decodeError := json.NewDecoder(response.Body).Decode(&decoded)
		response.Body.Close()
		if response.StatusCode != http.StatusOK {
			clientError := response.StatusCode >= 400 && response.StatusCode < 500 &&
				response.StatusCode != http.StatusTooManyRequests
			if clientError && decodeError == nil && decoded.Error != nil {
				return decoded.Error
			}
			lastError = fmt.Errorf("%s returned HTTP %s", method, response.Status)
			continue
		}
		if decodeError != nil {
			lastError = fmt.Errorf("decode %s: %w", method, decodeError)
			continue
		}
		if decoded.Error != nil {
			return decoded.Error
		}
		if result == nil {
			return nil
		}
		return json.Unmarshal(decoded.Result, result)
	}
	return fmt.Errorf("%s failed after %d attempts: %w", method, c.attempts, lastError)
}

// callRaw posts a prepared payload (for example a JSON-RPC batch) and decodes
// the whole response body, retrying transport failures.
func (c *rpcClient) callRaw(ctx context.Context, payload any, result any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	var lastError error
	for attempt := 0; attempt < c.attempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
		if err != nil {
			return err
		}
		request.Header.Set("Content-Type", "application/json")
		response, err := c.http.Do(request)
		if err != nil {
			lastError = err
			continue
		}
		decodeError := json.NewDecoder(response.Body).Decode(result)
		response.Body.Close()
		if response.StatusCode != http.StatusOK {
			lastError = fmt.Errorf("batch returned HTTP %s", response.Status)
			continue
		}
		if decodeError != nil {
			lastError = decodeError
			continue
		}
		return nil
	}
	return fmt.Errorf("batch failed after %d attempts: %w", c.attempts, lastError)
}

type rpcBlock struct {
	Number    string `json:"number"`
	Hash      string `json:"hash"`
	StateRoot string `json:"stateRoot"`
	Timestamp string `json:"timestamp"`
}

type rpcLog struct {
	Address         string   `json:"address"`
	Topics          []string `json:"topics"`
	Data            string   `json:"data"`
	BlockNumber     string   `json:"blockNumber"`
	TransactionHash string   `json:"transactionHash"`
	LogIndex        string   `json:"logIndex"`
	Removed         bool     `json:"removed"`
}

// logKey identifies a log by transaction and log index.
func logKey(log rpcLog) uint64 {
	hash := fnv.New64a()
	hash.Write([]byte(strings.ToLower(log.TransactionHash)))
	hash.Write([]byte{0})
	hash.Write([]byte(strings.ToLower(log.LogIndex)))
	return hash.Sum64()
}

type seenSet map[common.Address]map[uint64]bool

type repairedLog struct {
	Block           uint64 `json:"block"`
	TransactionHash string `json:"transaction_hash"`
	LogIndex        string `json:"log_index"`
	Reason          string `json:"reason"`
}

type scanStats struct {
	queries atomic.Uint64
	splits  atomic.Uint64
	events  atomic.Uint64
}

type spotCheck struct {
	Address string `json:"address"`
	Derived string `json:"derived_raw"`
	OnChain string `json:"balance_of_raw"`
}

type summary struct {
	SchemaVersion       int           `json:"schema_version"`
	Status              string        `json:"status"`
	SourceKind          string        `json:"source_kind"`
	RPCHost             string        `json:"rpc_host"`
	ChainID             uint64        `json:"chain_id"`
	Token               string        `json:"token"`
	TokenCodeHash       string        `json:"token_code_hash"`
	Decimals            *uint64       `json:"decimals"`
	Block               uint64        `json:"block"`
	BlockHash           string        `json:"block_hash"`
	BlockTimestamp      uint64        `json:"block_timestamp"`
	FromBlock           uint64        `json:"from_block"`
	RangeSize           uint64        `json:"range_size"`
	LogQueries          uint64        `json:"log_queries"`
	AdaptiveSplits      uint64        `json:"adaptive_splits"`
	TransferEvents      uint64        `json:"transfer_events"`
	HolderCount         uint64        `json:"holder_count"`
	HolderTotalRaw      string        `json:"holder_total_raw"`
	TotalSupplyRaw      string        `json:"total_supply_raw"`
	SupplyMinusTotalRaw string        `json:"supply_minus_holder_total_raw"`
	SpotChecks          []spotCheck   `json:"spot_checks"`
	VerifiedHolders     uint64        `json:"verified_holders"`
	ReplayMismatches    uint64        `json:"replay_mismatches_replaced_by_balance_of"`
	RepairedLogs        []repairedLog `json:"repaired_logs"`
	Problems            []string      `json:"problems"`
	Output              string        `json:"output"`
	OutputSHA256        string        `json:"output_sha256"`
	ElapsedMilliseconds int64         `json:"elapsed_milliseconds"`
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func hexQuantity(value uint64) string {
	return fmt.Sprintf("0x%x", value)
}

func parseUint(value string) (uint64, error) {
	if !strings.HasPrefix(value, "0x") {
		return 0, fmt.Errorf("quantity lacks 0x prefix: %q", value)
	}
	return strconv.ParseUint(strings.TrimPrefix(value, "0x"), 16, 64)
}

func parseBig(value string) (*big.Int, error) {
	raw := strings.TrimPrefix(value, "0x")
	if raw == "" || raw == value {
		return nil, fmt.Errorf("invalid hexadecimal value %q", value)
	}
	result, ok := new(big.Int).SetString(raw, 16)
	if !ok {
		return nil, fmt.Errorf("invalid hexadecimal value %q", value)
	}
	return result, nil
}

func parseHex(value string) ([]byte, error) {
	if !strings.HasPrefix(value, "0x") {
		return nil, errors.New("hex data lacks 0x prefix")
	}
	return hex.DecodeString(strings.TrimPrefix(value, "0x"))
}

func redactURL(endpoint string) string {
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return "unparseable"
	}
	return parsed.Scheme + "://" + parsed.Hostname()
}

func ethCall(ctx context.Context, client *rpcClient, to common.Address, data string, block uint64) (string, error) {
	var result string
	err := client.call(ctx, "eth_call", []any{
		map[string]string{"to": to.Hex(), "data": data},
		hexQuantity(block),
	}, &result)
	return result, err
}

func codeAt(ctx context.Context, client *rpcClient, contract common.Address, block uint64) ([]byte, error) {
	var result string
	if err := client.call(ctx, "eth_getCode", []any{contract.Hex(), hexQuantity(block)}, &result); err != nil {
		return nil, err
	}
	return parseHex(result)
}

func findDeploymentBlock(ctx context.Context, client *rpcClient, contract common.Address, block uint64) (uint64, error) {
	low, high := uint64(0), block
	for low < high {
		middle := low + (high-low)/2
		code, err := codeAt(ctx, client, contract, middle)
		if err != nil {
			return 0, fmt.Errorf("read code at block %d: %w", middle, err)
		}
		if len(code) == 0 {
			low = middle + 1
		} else {
			high = middle
		}
	}
	return low, nil
}

// fetchLogs reads Transfer logs of every listed token in one request per
// range, splitting the range when the RPC rejects it.
func fetchLogs(ctx context.Context, client *rpcClient, contracts []string, from, to uint64, stats *scanStats) ([]rpcLog, error) {
	return fetchTopicLogs(ctx, client, contracts, []any{transferTopic}, from, to, stats)
}

func fetchTopicLogs(ctx context.Context, client *rpcClient, contracts []string, topics []any, from, to uint64, stats *scanStats) ([]rpcLog, error) {
	stats.queries.Add(1)
	var logs []rpcLog
	err := client.call(ctx, "eth_getLogs", []any{map[string]any{
		"fromBlock": hexQuantity(from),
		"toBlock":   hexQuantity(to),
		"address":   contracts,
		"topics":    topics,
	}}, &logs)
	if err == nil {
		return logs, nil
	}
	if from == to {
		return nil, fmt.Errorf("fetch logs at block %d: %w", from, err)
	}
	stats.splits.Add(1)
	middle := from + (to-from)/2
	left, err := fetchTopicLogs(ctx, client, contracts, topics, from, middle, stats)
	if err != nil {
		return nil, err
	}
	right, err := fetchTopicLogs(ctx, client, contracts, topics, middle+1, to, stats)
	if err != nil {
		return nil, err
	}
	return append(left, right...), nil
}

func topicAddress(value string) (common.Address, error) {
	decoded, err := parseHex(value)
	if err != nil {
		return common.Address{}, err
	}
	if len(decoded) != common.HashLength {
		return common.Address{}, fmt.Errorf("address word has %d bytes", len(decoded))
	}
	for _, b := range decoded[:12] {
		if b != 0 {
			return common.Address{}, errors.New("address word has a nonzero high byte")
		}
	}
	return common.BytesToAddress(decoded[12:]), nil
}

// decodeTransfer accepts the standard form (from and to indexed) and the
// pre-standard form where from, to, and value are all in the data.
func decodeTransfer(log rpcLog) (common.Address, common.Address, *big.Int, error) {
	data, err := parseHex(log.Data)
	if err != nil {
		return common.Address{}, common.Address{}, nil, err
	}
	switch {
	case len(log.Topics) == 3 && len(data) == 32:
		source, err := topicAddress(log.Topics[1])
		if err != nil {
			return common.Address{}, common.Address{}, nil, err
		}
		destination, err := topicAddress(log.Topics[2])
		if err != nil {
			return common.Address{}, common.Address{}, nil, err
		}
		return source, destination, new(big.Int).SetBytes(data), nil
	case len(log.Topics) == 1 && len(data) == 96:
		source, err := topicAddress("0x" + hex.EncodeToString(data[:32]))
		if err != nil {
			return common.Address{}, common.Address{}, nil, err
		}
		destination, err := topicAddress("0x" + hex.EncodeToString(data[32:64]))
		if err != nil {
			return common.Address{}, common.Address{}, nil, err
		}
		return source, destination, new(big.Int).SetBytes(data[64:]), nil
	}
	return common.Address{}, common.Address{}, nil, fmt.Errorf(
		"unsupported Transfer layout: %d topics, %d data bytes", len(log.Topics), len(data))
}

type balanceSet map[common.Address]map[common.Address]*big.Int

// applyLog adds one Transfer to the balances of the token that emitted it.
func applyLog(log rpcLog, tokens map[common.Address]bool, sets balanceSet, seen seenSet) error {
	if log.Removed {
		return errors.New("RPC returned a removed log")
	}
	if !common.IsHexAddress(log.Address) {
		return fmt.Errorf("invalid log address %q", log.Address)
	}
	token := common.HexToAddress(log.Address)
	if !tokens[token] {
		return fmt.Errorf("unexpected log address %q", log.Address)
	}
	if seen != nil {
		keys := seen[token]
		if keys == nil {
			keys = make(map[uint64]bool)
			seen[token] = keys
		}
		key := logKey(log)
		if keys[key] {
			return nil
		}
		keys[key] = true
	}
	deltas := sets[token]
	if deltas == nil {
		deltas = make(map[common.Address]*big.Int)
		sets[token] = deltas
	}
	if len(log.Topics) == 0 || !strings.EqualFold(log.Topics[0], transferTopic) {
		return errors.New("log is not a Transfer event")
	}
	source, destination, amount, err := decodeTransfer(log)
	if err != nil {
		return err
	}
	if amount.Sign() == 0 {
		return nil
	}
	add := func(address common.Address, value *big.Int) {
		if address == (common.Address{}) {
			return
		}
		current := deltas[address]
		if current == nil {
			current = new(big.Int)
			deltas[address] = current
		}
		current.Add(current, value)
	}
	add(source, new(big.Int).Neg(amount))
	add(destination, amount)
	return nil
}

func scan(ctx context.Context, client *rpcClient, contracts []common.Address, from, to, rangeSize uint64, workers int, stats *scanStats) (balanceSet, seenSet, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	tokens := make(map[common.Address]bool, len(contracts))
	addresses := make([]string, 0, len(contracts))
	for _, contract := range contracts {
		tokens[contract] = true
		addresses = append(addresses, contract.Hex())
	}
	type job struct{ from, to uint64 }
	type rangeResult struct {
		deltas balanceSet
		seen   seenSet
	}
	jobs := make(chan job, workers*2)
	results := make(chan rangeResult, workers*2)
	failure := make(chan error, 1)
	var group sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for item := range jobs {
				logs, err := fetchLogs(ctx, client, addresses, item.from, item.to, stats)
				if err == nil {
					deltas := make(balanceSet)
					keys := make(seenSet)
					for _, log := range logs {
						if err = applyLog(log, tokens, deltas, keys); err != nil {
							break
						}
						stats.events.Add(1)
					}
					if err == nil {
						select {
						case results <- rangeResult{deltas, keys}:
							continue
						case <-ctx.Done():
							return
						}
					}
				}
				select {
				case failure <- err:
				default:
				}
				cancel()
				return
			}
		}()
	}
	total := (to-from)/rangeSize + 1
	go func() {
		defer close(jobs)
		for start := from; start <= to; start += rangeSize {
			end := start + rangeSize - 1
			if end > to || end < start {
				end = to
			}
			select {
			case jobs <- job{start, end}:
			case <-ctx.Done():
				return
			}
			if end == to {
				return
			}
		}
	}()
	go func() {
		group.Wait()
		close(results)
	}()
	balances := make(balanceSet)
	seen := make(seenSet)
	var completed uint64
	last := time.Now()
	for result := range results {
		for token, keys := range result.seen {
			all := seen[token]
			if all == nil {
				all = make(map[uint64]bool)
				seen[token] = all
			}
			for key := range keys {
				all[key] = true
			}
		}
		for token, changes := range result.deltas {
			held := balances[token]
			if held == nil {
				held = make(map[common.Address]*big.Int)
				balances[token] = held
			}
			for address, delta := range changes {
				current := held[address]
				if current == nil {
					current = new(big.Int)
					held[address] = current
				}
				current.Add(current, delta)
			}
		}
		completed++
		if time.Since(last) >= 10*time.Second {
			fmt.Fprintf(os.Stderr, "progress ranges=%d/%d queries=%d events=%d\n",
				completed, total, stats.queries.Load(), stats.events.Load())
			last = time.Now()
		}
	}
	select {
	case err := <-failure:
		return nil, nil, err
	default:
	}
	if completed != total {
		return nil, nil, fmt.Errorf("completed %d of %d ranges", completed, total)
	}
	return balances, seen, nil
}

// balancesAt reads balanceOf for every address at block with batched
// JSON-RPC requests, falling back to single calls when a batch fails.
func balancesAt(ctx context.Context, client *rpcClient, token common.Address, addresses []common.Address, block uint64, workers int) (map[common.Address]*big.Int, error) {
	const chunk = 50
	result := make(map[common.Address]*big.Int, len(addresses))
	var lock sync.Mutex
	var group sync.WaitGroup
	failure := make(chan error, 1)
	limit := make(chan struct{}, workers)
	for start := 0; start < len(addresses); start += chunk {
		end := start + chunk
		if end > len(addresses) {
			end = len(addresses)
		}
		part := addresses[start:end]
		group.Add(1)
		limit <- struct{}{}
		go func() {
			defer group.Done()
			defer func() { <-limit }()
			values, err := balanceBatch(ctx, client, token, part, block)
			if err != nil {
				select {
				case failure <- err:
				default:
				}
				return
			}
			lock.Lock()
			for address, value := range values {
				result[address] = value
			}
			lock.Unlock()
		}()
	}
	group.Wait()
	select {
	case err := <-failure:
		return nil, err
	default:
	}
	return result, nil
}

func balanceData(address common.Address) string {
	return balanceOfPrefix + strings.Repeat("0", 24) + strings.ToLower(address.Hex()[2:])
}

func balanceBatch(ctx context.Context, client *rpcClient, token common.Address, addresses []common.Address, block uint64) (map[common.Address]*big.Int, error) {
	requests := make([]map[string]any, len(addresses))
	for index, address := range addresses {
		requests[index] = map[string]any{
			"jsonrpc": "2.0",
			"id":      index,
			"method":  "eth_call",
			"params": []any{
				map[string]string{"to": token.Hex(), "data": balanceData(address)},
				hexQuantity(block),
			},
		}
	}
	values := make(map[common.Address]*big.Int, len(addresses))
	var responses []struct {
		ID     int             `json:"id"`
		Result json.RawMessage `json:"result"`
		Error  *rpcError       `json:"error"`
	}
	if err := client.callRaw(ctx, requests, &responses); err == nil && len(responses) == len(addresses) {
		for _, response := range responses {
			if response.Error != nil || response.ID < 0 || response.ID >= len(addresses) {
				values = nil
				break
			}
			var text string
			if json.Unmarshal(response.Result, &text) != nil {
				values = nil
				break
			}
			value, err := parseBig(text)
			if err != nil {
				values = nil
				break
			}
			values[addresses[response.ID]] = value
		}
		if values != nil && len(values) == len(addresses) {
			return values, nil
		}
	}
	values = make(map[common.Address]*big.Int, len(addresses))
	for _, address := range addresses {
		text, err := ethCall(ctx, client, token, balanceData(address), block)
		if err != nil {
			return nil, fmt.Errorf("balanceOf %s: %w", address.Hex(), err)
		}
		value, err := parseBig(text)
		if err != nil {
			return nil, fmt.Errorf("decode balanceOf %s: %w", address.Hex(), err)
		}
		values[address] = value
	}
	return values, nil
}

// addressLogs reads every Transfer from or to address in [from, to], with
// ranges fetched concurrently.
func addressLogs(ctx context.Context, client *rpcClient, token, address common.Address, from, to, rangeSize uint64, workers int, stats *scanStats) ([]rpcLog, error) {
	word := "0x" + strings.Repeat("0", 24) + strings.ToLower(address.Hex()[2:])
	type job struct {
		topics   []any
		from, to uint64
	}
	var jobs []job
	for _, topics := range [][]any{{transferTopic, word}, {transferTopic, nil, word}} {
		for start := from; start <= to; start += rangeSize {
			end := start + rangeSize - 1
			if end > to || end < start {
				end = to
			}
			jobs = append(jobs, job{topics, start, end})
			if end == to {
				break
			}
		}
	}
	var lock sync.Mutex
	var logs []rpcLog
	var firstError error
	queue := make(chan job)
	var group sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for item := range queue {
				part, err := fetchTopicLogs(ctx, client, []string{token.Hex()}, item.topics, item.from, item.to, stats)
				lock.Lock()
				if err != nil && firstError == nil {
					firstError = err
				}
				logs = append(logs, part...)
				lock.Unlock()
			}
		}()
	}
	for _, item := range jobs {
		queue <- item
	}
	close(queue)
	group.Wait()
	if firstError != nil {
		return nil, firstError
	}
	return logs, nil
}

// repairAddress finds Transfer logs that the range scan missed for one
// address whose replayed balance differs from balanceOf. Logs returned by an
// address-filtered query but absent from the scan are applied directly;
// otherwise the first block where the address's own replay diverges from
// balanceOf is re-read in full.
func repairAddress(ctx context.Context, client *rpcClient, token, address common.Address, from, to, rangeSize uint64,
	workers int, balances map[common.Address]*big.Int, seen map[uint64]bool, stats *scanStats) ([]repairedLog, error) {
	tokens := map[common.Address]bool{token: true}
	apply := func(log rpcLog, reason string) (repairedLog, bool, error) {
		if seen[logKey(log)] {
			return repairedLog{}, false, nil
		}
		sets := balanceSet{token: balances}
		if err := applyLog(log, tokens, sets, seenSet{token: seen}); err != nil {
			return repairedLog{}, false, err
		}
		block, _ := parseUint(log.BlockNumber)
		return repairedLog{block, log.TransactionHash, log.LogIndex, reason}, true, nil
	}
	own, err := addressLogs(ctx, client, token, address, from, to, rangeSize, workers, stats)
	if err != nil {
		return nil, err
	}
	var repaired []repairedLog
	for _, log := range own {
		item, applied, err := apply(log, "returned by an address-filtered query")
		if err != nil {
			return nil, err
		}
		if applied {
			repaired = append(repaired, item)
		}
	}
	if len(repaired) > 0 {
		return repaired, nil
	}
	type change struct {
		block uint64
		index uint64
		delta *big.Int
	}
	var changes []change
	unique := make(map[uint64]bool)
	for _, log := range own {
		key := logKey(log)
		if unique[key] {
			continue
		}
		unique[key] = true
		source, destination, amount, err := decodeTransfer(log)
		if err != nil {
			return nil, err
		}
		block, _ := parseUint(log.BlockNumber)
		index, _ := parseUint(log.LogIndex)
		delta := new(big.Int)
		if source == address {
			delta.Sub(delta, amount)
		}
		if destination == address {
			delta.Add(delta, amount)
		}
		changes = append(changes, change{block, index, delta})
	}
	sort.Slice(changes, func(i, j int) bool {
		if changes[i].block != changes[j].block {
			return changes[i].block < changes[j].block
		}
		return changes[i].index < changes[j].index
	})
	replayed := func(block uint64) *big.Int {
		total := new(big.Int)
		for _, item := range changes {
			if item.block > block {
				break
			}
			total.Add(total, item.delta)
		}
		return total
	}
	onChain := func(block uint64) (*big.Int, error) {
		values, err := balanceBatch(ctx, client, token, []common.Address{address}, block)
		if err != nil {
			return nil, err
		}
		return values[address], nil
	}
	low, high := from, to
	first, err := onChain(low)
	if err != nil {
		return nil, err
	}
	if first.Cmp(replayed(low)) != 0 {
		high = low
	} else {
		for high-low > 1 {
			middle := low + (high-low)/2
			value, err := onChain(middle)
			if err != nil {
				return nil, err
			}
			if value.Cmp(replayed(middle)) == 0 {
				low = middle
			} else {
				high = middle
			}
		}
	}
	block, err := fetchLogs(ctx, client, []string{token.Hex()}, high, high, stats)
	if err != nil {
		return nil, err
	}
	for _, log := range block {
		item, applied, err := apply(log, "found by re-reading the first divergent block")
		if err != nil {
			return nil, err
		}
		if applied {
			repaired = append(repaired, item)
		}
	}
	return repaired, nil
}

func formatUnits(value *big.Int, decimals *uint64) string {
	if decimals == nil {
		return ""
	}
	divisor := new(big.Int).Exp(big.NewInt(10), new(big.Int).SetUint64(*decimals), nil)
	whole, fraction := new(big.Int).QuoRem(value, divisor, new(big.Int))
	if *decimals == 0 {
		return whole.String()
	}
	text := fraction.String()
	return whole.String() + "." + strings.Repeat("0", int(*decimals)-len(text)) + text
}

func writeCSV(path string, addresses []common.Address, balances map[common.Address]*big.Int, decimals *uint64) (string, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	partial := path + ".partial"
	output, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return "", err
	}
	complete := false
	defer func() {
		output.Close()
		if !complete {
			os.Remove(partial)
		}
	}()
	digest := sha256.New()
	buffer := bufio.NewWriterSize(io.MultiWriter(output, digest), 4*1024*1024)
	writer := csv.NewWriter(buffer)
	if err := writer.Write([]string{"address", "balance_raw", "balance"}); err != nil {
		return "", err
	}
	for _, address := range addresses {
		balance := balances[address]
		if err := writer.Write([]string{
			strings.ToLower(address.Hex()), balance.String(), formatUnits(balance, decimals),
		}); err != nil {
			return "", err
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		return "", err
	}
	if err := buffer.Flush(); err != nil {
		return "", err
	}
	if err := output.Sync(); err != nil {
		return "", err
	}
	if err := output.Close(); err != nil {
		return "", err
	}
	if err := os.Rename(partial, path); err != nil {
		return "", err
	}
	complete = true
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func writeJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	partial := path + ".partial"
	if err := os.WriteFile(partial, append(encoded, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(partial, path)
}

// sampleAddresses returns the largest holders followed by a deterministic
// spread of other holders chosen by hashing each address.
func sampleAddresses(addresses []common.Address, balances map[common.Address]*big.Int, top, spread int) []common.Address {
	byBalance := append([]common.Address(nil), addresses...)
	sort.Slice(byBalance, func(i, j int) bool {
		if c := balances[byBalance[i]].Cmp(balances[byBalance[j]]); c != 0 {
			return c > 0
		}
		return bytes.Compare(byBalance[i][:], byBalance[j][:]) < 0
	})
	chosen := make(map[common.Address]bool)
	var result []common.Address
	for _, address := range byBalance {
		if len(result) >= top {
			break
		}
		chosen[address] = true
		result = append(result, address)
	}
	byHash := append([]common.Address(nil), addresses...)
	sort.Slice(byHash, func(i, j int) bool {
		return bytes.Compare(crypto.Keccak256(byHash[i][:]), crypto.Keccak256(byHash[j][:])) < 0
	})
	for _, address := range byHash {
		if len(result) >= top+spread {
			break
		}
		if !chosen[address] {
			result = append(result, address)
		}
	}
	return result
}

type scanCache struct {
	Tokens   []string
	From     uint64
	Block    uint64
	Balances balanceSet
	Seen     seenSet
}

// cachedScan runs scan, or reuses a saved result for exactly the same
// tokens and block range.
func cachedScan(ctx context.Context, client *rpcClient, path string, contracts []common.Address, from, to, rangeSize uint64,
	workers int, stats *scanStats) (balanceSet, seenSet, error) {
	tokens := make([]string, 0, len(contracts))
	for _, contract := range contracts {
		tokens = append(tokens, strings.ToLower(contract.Hex()))
	}
	sort.Strings(tokens)
	if path != "" {
		if file, err := os.Open(path); err == nil {
			var cached scanCache
			decodeError := gob.NewDecoder(bufio.NewReader(file)).Decode(&cached)
			file.Close()
			if decodeError != nil {
				return nil, nil, fmt.Errorf("read scan cache: %w", decodeError)
			}
			if strings.Join(cached.Tokens, ",") != strings.Join(tokens, ",") || cached.From != from || cached.Block != to {
				return nil, nil, errors.New("scan cache was made for other tokens or blocks")
			}
			fmt.Fprintf(os.Stderr, "reusing scan cache %s\n", path)
			return cached.Balances, cached.Seen, nil
		}
	}
	balances, seen, err := scan(ctx, client, contracts, from, to, rangeSize, workers, stats)
	if err != nil || path == "" {
		return balances, seen, err
	}
	partial := path + ".partial"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, nil, err
	}
	writer := bufio.NewWriter(file)
	if err := gob.NewEncoder(writer).Encode(scanCache{tokens, from, to, balances, seen}); err != nil {
		file.Close()
		return nil, nil, err
	}
	if err := writer.Flush(); err != nil {
		file.Close()
		return nil, nil, err
	}
	if err := file.Close(); err != nil {
		return nil, nil, err
	}
	return balances, seen, os.Rename(partial, path)
}

type target struct {
	token      common.Address
	code       []byte
	deployment uint64
	output     string
	summary    string
}

// finalize checks one token's reconstructed balances at the snapshot block
// and writes its holder CSV and summary. It returns the problems found.
type options struct {
	verifyTop     int
	verifySpread  int
	verifyAll     bool
	repair        bool
	repairRounds  int
	allowMismatch bool
	workers       int
}

// verifyAndRepair replaces every replayed balance with balanceOf at the
// snapshot block for every address that ever appeared in a Transfer, and
// accepts the result only when those balances sum exactly to totalSupply.
// Because no balance is negative, an exact sum proves no other address holds
// the token. When the sum falls short, an address must be missing from the
// replay, so the logs the range scan missed are located from the accounts
// whose replay disagrees with balanceOf, and the check repeats.
func verifyAndRepair(ctx context.Context, client *rpcClient, item target, balances map[common.Address]*big.Int,
	seen map[uint64]bool, totalSupply *big.Int, base summary, opts options, stats *scanStats) (uint64, []repairedLog, []string, error) {
	repaired := []repairedLog{}
	for round := 0; ; round++ {
		candidates := make([]common.Address, 0, len(balances))
		for address := range balances {
			candidates = append(candidates, address)
		}
		sort.Slice(candidates, func(i, j int) bool { return bytes.Compare(candidates[i][:], candidates[j][:]) < 0 })
		onChain, err := balancesAt(ctx, client, item.token, candidates, base.Block, opts.workers)
		if err != nil {
			return 0, repaired, nil, err
		}
		total := new(big.Int)
		type gap struct {
			address common.Address
			size    *big.Int
		}
		var gaps []gap
		for _, address := range candidates {
			total.Add(total, onChain[address])
			if difference := new(big.Int).Sub(onChain[address], balances[address]); difference.Sign() != 0 {
				gaps = append(gaps, gap{address, difference.Abs(difference)})
			}
		}
		if total.Cmp(totalSupply) == 0 {
			for _, address := range candidates {
				balances[address] = onChain[address]
			}
			return uint64(len(gaps)), repaired, nil, nil
		}
		problem := fmt.Sprintf("balanceOf of %d replay addresses sums to %s, totalSupply is %s",
			len(candidates), total, totalSupply)
		if !opts.repair || round >= opts.repairRounds || len(gaps) == 0 {
			return 0, repaired, []string{problem}, nil
		}
		sort.Slice(gaps, func(i, j int) bool {
			if c := gaps[i].size.Cmp(gaps[j].size); c != 0 {
				return c > 0
			}
			return bytes.Compare(gaps[i].address[:], gaps[j].address[:]) < 0
		})
		fixed := false
		for _, candidate := range gaps {
			found, err := repairAddress(ctx, client, item.token, candidate.address, item.deployment, base.Block,
				base.RangeSize, opts.workers, balances, seen, stats)
			if err != nil {
				return 0, repaired, nil, err
			}
			if len(found) > 0 {
				for _, log := range found {
					fmt.Fprintf(os.Stderr, "%s: repaired missed log %s:%s at block %d (%s)\n",
						item.token.Hex(), log.TransactionHash, log.LogIndex, log.Block, log.Reason)
				}
				repaired = append(repaired, found...)
				fixed = true
				break
			}
		}
		if !fixed {
			return 0, repaired, []string{problem + "; no missed log could be located"}, nil
		}
	}
}

func finalize(ctx context.Context, client *rpcClient, item target, balances map[common.Address]*big.Int,
	seen map[uint64]bool, base summary, opts options, stats *scanStats) ([]string, error) {
	block := base.Block
	supplyText, err := ethCall(ctx, client, item.token, totalSupplyCall, block)
	if err != nil {
		return nil, fmt.Errorf("read totalSupply: %w", err)
	}
	totalSupply, err := parseBig(supplyText)
	if err != nil {
		return nil, fmt.Errorf("decode totalSupply: %w", err)
	}
	var decimals *uint64
	if text, err := ethCall(ctx, client, item.token, decimalsCall, block); err == nil {
		if value, err := parseBig(text); err == nil && value.IsUint64() {
			decoded := value.Uint64()
			decimals = &decoded
		}
	}
	var problems []string
	var replayMismatches uint64
	var repaired []repairedLog
	allRead := false
	if opts.verifyAll {
		mismatches, found, issues, err := verifyAndRepair(ctx, client, item, balances, seen, totalSupply, base, opts, stats)
		if err != nil {
			return nil, err
		}
		replayMismatches, repaired = mismatches, found
		problems = append(problems, issues...)
		allRead = len(issues) == 0
	}
	addresses := make([]common.Address, 0, len(balances))
	total := new(big.Int)
	for address, balance := range balances {
		switch balance.Sign() {
		case -1:
			problems = append(problems, fmt.Sprintf("negative balance for %s: %s", address.Hex(), balance))
		case 1:
			addresses = append(addresses, address)
			total.Add(total, balance)
		}
	}
	sort.Slice(addresses, func(i, j int) bool {
		return bytes.Compare(addresses[i][:], addresses[j][:]) < 0
	})
	difference := new(big.Int).Sub(totalSupply, total)
	if difference.Sign() != 0 {
		problems = append(problems, fmt.Sprintf("totalSupply %s minus holder total %s is %s", totalSupply, total, difference))
	}
	var checks []spotCheck
	samples := sampleAddresses(addresses, balances, opts.verifyTop, opts.verifySpread)
	if opts.verifyAll {
		samples = nil
	}
	for _, address := range samples {
		text, err := ethCall(ctx, client, item.token, balanceOfPrefix+strings.Repeat("0", 24)+strings.ToLower(address.Hex()[2:]), block)
		if err != nil {
			return nil, fmt.Errorf("balanceOf %s: %w", address.Hex(), err)
		}
		value, err := parseBig(text)
		if err != nil {
			return nil, fmt.Errorf("decode balanceOf %s: %w", address.Hex(), err)
		}
		checks = append(checks, spotCheck{strings.ToLower(address.Hex()), balances[address].String(), value.String()})
		if value.Cmp(balances[address]) != 0 {
			problems = append(problems, fmt.Sprintf("balanceOf %s is %s, events give %s", address.Hex(), value, balances[address]))
		}
	}
	if len(problems) > 0 && !opts.allowMismatch {
		return problems, nil
	}
	digest, err := writeCSV(item.output, addresses, balances, decimals)
	if err != nil {
		return nil, fmt.Errorf("write CSV: %w", err)
	}
	result := base
	result.Status = "passed"
	if len(problems) > 0 {
		result.Status = "diagnostic_mismatch"
	}
	result.Token = strings.ToLower(item.token.Hex())
	result.TokenCodeHash = crypto.Keccak256Hash(item.code).Hex()
	result.Decimals = decimals
	result.HolderCount = uint64(len(addresses))
	result.HolderTotalRaw = total.String()
	result.TotalSupplyRaw = totalSupply.String()
	result.SupplyMinusTotalRaw = difference.String()
	result.SpotChecks = checks
	if allRead {
		result.VerifiedHolders = uint64(len(addresses))
	}
	result.ReplayMismatches = replayMismatches
	result.RepairedLogs = repaired
	result.Problems = problems
	result.Output = filepath.Base(item.output)
	result.OutputSHA256 = digest
	if err := writeJSON(item.summary, result); err != nil {
		return nil, fmt.Errorf("write summary: %w", err)
	}
	fmt.Printf("%s holders=%d total=%s supply=%s status=%s\n",
		item.token.Hex(), len(addresses), total, totalSupply, result.Status)
	return problems, nil
}

func main() {
	var (
		rpcURL        = flag.String("rpc", "", "archive JSON-RPC URL")
		chainID       = flag.Uint64("chain-id", 0, "expected eth_chainId")
		tokenText     = flag.String("token", "", "ERC-20 contract address (single-token mode)")
		tokensText    = flag.String("tokens", "", "comma-separated ERC-20 addresses scanned in one pass")
		block         = flag.Uint64("block", 0, "inclusive snapshot block")
		blockHash     = flag.String("block-hash", "", "expected snapshot block hash")
		fromBlock     = flag.Uint64("from-block", 0, "first block to scan; 0 finds the earliest deployment block")
		rangeSize     = flag.Uint64("range-size", 2000, "initial eth_getLogs range size")
		workers       = flag.Int("workers", 8, "concurrent eth_getLogs workers")
		attempts      = flag.Int("attempts", 8, "attempts per RPC request")
		verifyAll     = flag.Bool("verify-all", true, "check every replayed balance with balanceOf")
		repair        = flag.Bool("repair", true, "locate and apply Transfer logs the range scan missed")
		repairRounds  = flag.Int("repair-rounds", 200, "maximum repair rounds per token")
		verifyTop     = flag.Int("verify-top", 25, "largest holders checked with balanceOf when -verify-all=false")
		verifySpread  = flag.Int("verify-spread", 25, "other holders checked with balanceOf when -verify-all=false")
		allowMismatch = flag.Bool("allow-supply-mismatch", false, "write a marked diagnostic result instead of failing")
		outputPath    = flag.String("output", "", "holder CSV output (single-token mode)")
		summaryPath   = flag.String("summary", "", "summary JSON output (single-token mode)")
		outputDir     = flag.String("output-dir", "", "directory for <prefix>-<token>-<block>.csv and .summary.json (-tokens mode)")
		namePrefix    = flag.String("name-prefix", "", "file name prefix, normally the chain name (-tokens mode)")
		scanCachePath = flag.String("scan-cache", "", "reuse or save the range-scan result for the same tokens and blocks")
	)
	flag.Parse()
	single := *tokenText != ""
	multi := *tokensText != ""
	if *rpcURL == "" || *chainID == 0 || *block == 0 || *blockHash == "" || *rangeSize == 0 || *workers <= 0 ||
		single == multi ||
		(single && (*outputPath == "" || *summaryPath == "")) ||
		(multi && (*outputDir == "" || *namePrefix == "")) {
		flag.Usage()
		os.Exit(2)
	}
	var targets []target
	addTarget := func(text, output, summaryFile string) {
		text = strings.TrimSpace(text)
		if !common.IsHexAddress(text) {
			fatalf("invalid token address %q", text)
		}
		for _, path := range []string{output, summaryFile} {
			if _, err := os.Stat(path); err == nil {
				fatalf("%s exists", path)
			}
		}
		targets = append(targets, target{token: common.HexToAddress(text), output: output, summary: summaryFile})
	}
	if single {
		addTarget(*tokenText, *outputPath, *summaryPath)
	} else {
		seen := make(map[string]bool)
		for _, text := range strings.Split(*tokensText, ",") {
			key := strings.ToLower(strings.TrimSpace(text))
			if key == "" || seen[key] {
				continue
			}
			seen[key] = true
			stem := fmt.Sprintf("%s-%s-%d", *namePrefix, key, *block)
			addTarget(key, filepath.Join(*outputDir, stem+".csv"), filepath.Join(*outputDir, stem+".summary.json"))
		}
	}
	ctx := context.Background()
	client := newRPCClient(*rpcURL, *attempts)
	started := time.Now()

	var chainText string
	if err := client.call(ctx, "eth_chainId", []any{}, &chainText); err != nil {
		fatalf("read chain id: %v", err)
	}
	if actual, err := parseUint(chainText); err != nil || actual != *chainID {
		fatalf("RPC chain id %s does not match %d", chainText, *chainID)
	}
	var header rpcBlock
	if err := client.call(ctx, "eth_getBlockByNumber", []any{hexQuantity(*block), false}, &header); err != nil {
		fatalf("read block: %v", err)
	}
	if !strings.EqualFold(header.Hash, *blockHash) {
		fatalf("block hash mismatch: got %s want %s", header.Hash, *blockHash)
	}
	timestamp, err := parseUint(header.Timestamp)
	if err != nil {
		fatalf("decode block timestamp: %v", err)
	}
	start := *fromBlock
	searchDeployment := start == 0
	if searchDeployment {
		start = *block
	}
	contracts := make([]common.Address, 0, len(targets))
	for index := range targets {
		code, err := codeAt(ctx, client, targets[index].token, *block)
		if err != nil {
			fatalf("read code of %s: %v", targets[index].token.Hex(), err)
		}
		if len(code) == 0 {
			fatalf("%s has no code at block %d", targets[index].token.Hex(), *block)
		}
		targets[index].code = code
		contracts = append(contracts, targets[index].token)
		targets[index].deployment = *fromBlock
		if searchDeployment {
			deployed, err := findDeploymentBlock(ctx, client, targets[index].token, *block)
			if err != nil {
				fatalf("find deployment of %s: %v", targets[index].token.Hex(), err)
			}
			targets[index].deployment = deployed
			if deployed < start {
				start = deployed
			}
		}
	}
	fmt.Fprintf(os.Stderr, "scanning %d tokens blocks %d..%d\n", len(targets), start, *block)

	var stats scanStats
	balances, seen, err := cachedScan(ctx, client, *scanCachePath, contracts, start, *block, *rangeSize, *workers, &stats)
	if err != nil {
		fatalf("scan Transfer events: %v", err)
	}
	base := summary{
		SchemaVersion:  1,
		SourceKind:     "archive eth_getLogs Transfer replay; every balance checked with balanceOf and the total with totalSupply at the snapshot block",
		RPCHost:        redactURL(*rpcURL),
		ChainID:        *chainID,
		Block:          *block,
		BlockHash:      strings.ToLower(header.Hash),
		BlockTimestamp: timestamp,
		FromBlock:      start,
		RangeSize:      *rangeSize,
		LogQueries:     stats.queries.Load(),
		AdaptiveSplits: stats.splits.Load(),
		TransferEvents: stats.events.Load(),
	}
	var failed []string
	for _, item := range targets {
		held := balances[item.token]
		if held == nil {
			held = make(map[common.Address]*big.Int)
		}
		result := base
		result.ElapsedMilliseconds = time.Since(started).Milliseconds()
		keys := seen[item.token]
		if keys == nil {
			keys = make(map[uint64]bool)
		}
		opts := options{*verifyTop, *verifySpread, *verifyAll, *repair, *repairRounds, *allowMismatch, *workers}
		problems, err := finalize(ctx, client, item, held, keys, result, opts, &stats)
		if err != nil {
			fatalf("%s: %v", item.token.Hex(), err)
		}
		if len(problems) > 0 && !*allowMismatch {
			for _, problem := range problems {
				fmt.Fprintf(os.Stderr, "%s: %s\n", item.token.Hex(), problem)
			}
			failed = append(failed, item.token.Hex())
		}
	}
	if len(failed) > 0 {
		fatalf("holder reconstruction failed for %s; rerun with -allow-supply-mismatch for marked diagnostics",
			strings.Join(failed, ", "))
	}
}
