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
	"os"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
)

type fileList []string

func (values *fileList) String() string {
	return strings.Join(*values, ",")
}

func (values *fileList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

type summary struct {
	InputPath           string   `json:"input_path"`
	MatchPaths          []string `json:"match_paths"`
	OutputPath          string   `json:"output_path"`
	OutputSHA256        string   `json:"output_sha256"`
	Rows                uint64   `json:"rows"`
	LoadedMatches       uint64   `json:"loaded_matches"`
	InitiallyResolved   uint64   `json:"initially_resolved"`
	InitiallyUnresolved uint64   `json:"initially_unresolved"`
	AppliedMatches      uint64   `json:"applied_matches"`
	Unresolved          uint64   `json:"unresolved"`
	UnusedMatches       uint64   `json:"unused_matches"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseHash(value string) (common.Hash, error) {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		return common.Hash{}, fmt.Errorf("invalid secure key %q", value)
	}
	return common.BytesToHash(decoded), nil
}

func verifyAddress(key common.Hash, value string) (common.Address, error) {
	if !common.IsHexAddress(value) {
		return common.Address{}, fmt.Errorf("invalid address %q", value)
	}
	address := common.HexToAddress(value)
	if crypto.Keccak256Hash(address.Bytes()) != key {
		return common.Address{}, fmt.Errorf("address %s does not match secure key %s", value, key.Hex())
	}
	return address, nil
}

func indexes(header []string) map[string]int {
	result := make(map[string]int, len(header))
	for index, name := range header {
		if _, duplicate := result[name]; duplicate {
			fatalf("duplicate CSV column %q", name)
		}
		result[name] = index
	}
	return result
}

func loadMatches(paths []string) map[common.Hash]common.Address {
	result := make(map[common.Hash]common.Address)
	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			fatalf("open match CSV %s: %v", path, err)
		}
		reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
		header, err := reader.Read()
		if err != nil {
			file.Close()
			fatalf("read match header %s: %v", path, err)
		}
		columns := indexes(header)
		keyColumn, haveKey := columns["secure_key"]
		addressColumn, haveAddress := columns["address"]
		if !haveKey || !haveAddress {
			file.Close()
			fatalf("match CSV %s must contain secure_key and address", path)
		}
		for line := 2; ; line++ {
			row, err := reader.Read()
			if err == io.EOF {
				break
			}
			if err != nil {
				file.Close()
				fatalf("read match CSV %s row %d: %v", path, line, err)
			}
			key, err := parseHash(row[keyColumn])
			if err != nil {
				file.Close()
				fatalf("match CSV %s row %d: %v", path, line, err)
			}
			address, err := verifyAddress(key, row[addressColumn])
			if err != nil {
				file.Close()
				fatalf("match CSV %s row %d: %v", path, line, err)
			}
			if prior, duplicate := result[key]; duplicate && prior != address {
				file.Close()
				fatalf("conflicting addresses for secure key %s", key.Hex())
			}
			result[key] = address
		}
		if err := file.Close(); err != nil {
			fatalf("close match CSV %s: %v", path, err)
		}
	}
	return result
}

func main() {
	var matchPaths fileList
	var (
		inputPath   = flag.String("input", "", "claim CSV to update")
		outputPath  = flag.String("output", "", "updated claim CSV")
		summaryPath = flag.String("summary", "", "summary JSON")
	)
	flag.Var(&matchPaths, "match", "verified secure-key/address match CSV; repeatable")
	flag.Parse()
	if *inputPath == "" || *outputPath == "" || *summaryPath == "" || len(matchPaths) == 0 {
		flag.Usage()
		os.Exit(2)
	}
	if *inputPath == *outputPath {
		fatalf("input and output paths must differ")
	}

	matches := loadMatches(matchPaths)
	used := make(map[common.Hash]struct{})
	input, err := os.Open(*inputPath)
	if err != nil {
		fatalf("open input: %v", err)
	}
	defer input.Close()
	reader := csv.NewReader(bufio.NewReaderSize(input, 4*1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read input header: %v", err)
	}
	columns := indexes(header)
	keyColumn, haveKey := columns["secure_key"]
	addressColumn, haveAddress := columns["address"]
	if !haveKey || !haveAddress {
		fatalf("input CSV must contain secure_key and address")
	}
	addressOrKeyColumn, haveAddressOrKey := columns["address_or_secure_key"]
	resolvedColumn, haveResolved := columns["address_resolved"]

	partialOutput := *outputPath + ".partial"
	output, err := os.OpenFile(partialOutput, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(io.MultiWriter(output, hasher), 4*1024*1024)
	writer := csv.NewWriter(buffered)
	if err := writer.Write(header); err != nil {
		fatalf("write output header: %v", err)
	}

	var rows, initiallyResolved, initiallyUnresolved, applied, unresolved uint64
	for line := 2; ; line++ {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read input row %d: %v", line, err)
		}
		if len(row) != len(header) {
			fatalf("input row %d has %d fields, expected %d", line, len(row), len(header))
		}
		rows++
		key, err := parseHash(row[keyColumn])
		if err != nil {
			fatalf("input row %d: %v", line, err)
		}
		if row[addressColumn] != "" {
			initiallyResolved++
			address, err := verifyAddress(key, row[addressColumn])
			if err != nil {
				fatalf("input row %d: %v", line, err)
			}
			if matched, ok := matches[key]; ok {
				if matched != address {
					fatalf("input row %d conflicts with matched address", line)
				}
				used[key] = struct{}{}
			}
		} else {
			initiallyUnresolved++
			if address, ok := matches[key]; ok {
				row[addressColumn] = address.Hex()
				if haveAddressOrKey {
					row[addressOrKeyColumn] = address.Hex()
				}
				if haveResolved {
					row[resolvedColumn] = "true"
				}
				used[key] = struct{}{}
				applied++
			} else {
				unresolved++
			}
		}
		if err := writer.Write(row); err != nil {
			fatalf("write output row %d: %v", line, err)
		}
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush output: %v", err)
	}
	if err := output.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := output.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partialOutput, *outputPath); err != nil {
		fatalf("publish output: %v", err)
	}

	result := summary{
		InputPath:           *inputPath,
		MatchPaths:          matchPaths,
		OutputPath:          *outputPath,
		OutputSHA256:        hex.EncodeToString(hasher.Sum(nil)),
		Rows:                rows,
		LoadedMatches:       uint64(len(matches)),
		InitiallyResolved:   initiallyResolved,
		InitiallyUnresolved: initiallyUnresolved,
		AppliedMatches:      applied,
		Unresolved:          unresolved,
		UnusedMatches:       uint64(len(matches) - len(used)),
	}
	data, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fatalf("encode summary: %v", err)
	}
	data = append(data, '\n')
	if err := os.WriteFile(*summaryPath+".partial", data, 0o600); err != nil {
		fatalf("write summary: %v", err)
	}
	if err := os.Rename(*summaryPath+".partial", *summaryPath); err != nil {
		fatalf("publish summary: %v", err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary output: %v", err)
	}
	if unresolved != 0 {
		os.Exit(3)
	}
}
