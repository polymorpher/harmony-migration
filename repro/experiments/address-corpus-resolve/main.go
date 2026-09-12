package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/harmony-one/harmony/internal/bech32"
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
	TargetsPath         string   `json:"targets_path"`
	SourcePaths         []string `json:"source_paths"`
	OutputPath          string   `json:"output_path"`
	OutputSHA256        string   `json:"output_sha256"`
	InitiallyUnresolved uint64   `json:"initially_unresolved"`
	FilesScanned        uint64   `json:"files_scanned"`
	BytesScanned        uint64   `json:"bytes_scanned"`
	HexCandidates       uint64   `json:"hex_candidates"`
	Bech32Candidates    uint64   `json:"bech32_candidates"`
	UniqueCandidates    uint64   `json:"unique_candidates"`
	Matches             uint64   `json:"matches"`
	Unresolved          uint64   `json:"unresolved"`
	ElapsedMilliseconds int64    `json:"elapsed_milliseconds"`
}

var (
	hexPattern    = regexp.MustCompile(`0x[0-9a-fA-F]{40}`)
	bech32Pattern = regexp.MustCompile(`(?:one|tone)1[023456789acdefghjklmnpqrstuvwxyz]{38}`)
	outputHeader  = []string{
		"secure_key",
		"address",
		"source",
		"block_number",
		"transaction_hash",
		"detail",
	}
)

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

func indexes(header []string) map[string]int {
	result := make(map[string]int, len(header))
	for index, name := range header {
		result[name] = index
	}
	return result
}

func loadTargets(path string) map[common.Hash]struct{} {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open targets: %v", err)
	}
	defer file.Close()
	reader := csv.NewReader(bufio.NewReaderSize(file, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read target header: %v", err)
	}
	columns := indexes(header)
	keyColumn, haveKey := columns["secure_key"]
	addressColumn, haveAddress := columns["address"]
	if !haveKey || !haveAddress {
		fatalf("target CSV must contain secure_key and address")
	}
	result := make(map[common.Hash]struct{})
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read targets: %v", err)
		}
		if row[addressColumn] != "" {
			continue
		}
		key, err := parseHash(row[keyColumn])
		if err != nil {
			fatalf("read targets: %v", err)
		}
		result[key] = struct{}{}
	}
	return result
}

func fileSHA256(path string) string {
	file, err := os.Open(path)
	if err != nil {
		fatalf("open output for hashing: %v", err)
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		fatalf("hash output: %v", err)
	}
	return hex.EncodeToString(hasher.Sum(nil))
}

func main() {
	var sources fileList
	var (
		targetsPath = flag.String("targets", "", "claim CSV containing unresolved secure keys")
		outputPath  = flag.String("output", "", "verified candidate matches CSV")
		summaryPath = flag.String("summary", "", "summary JSON")
		maxFileSize = flag.Int64("max-file-size", 128*1024*1024, "maximum source file size")
	)
	flag.Var(&sources, "source", "canonical source file or directory; repeatable")
	flag.Parse()
	if *targetsPath == "" || *outputPath == "" || *summaryPath == "" || len(sources) == 0 {
		flag.Usage()
		os.Exit(2)
	}

	started := time.Now()
	targets := loadTargets(*targetsPath)
	initiallyUnresolved := uint64(len(targets))
	seen := make(map[common.Address]struct{})
	partialOutput := *outputPath + ".partial"
	output, err := os.OpenFile(partialOutput, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(io.MultiWriter(output, hasher), 1024*1024)
	writer := csv.NewWriter(buffered)
	if err := writer.Write(outputHeader); err != nil {
		fatalf("write output header: %v", err)
	}

	var filesScanned, bytesScanned, hexCandidates, bech32Candidates, matches uint64
	add := func(address common.Address, path, encoding string, offset int) {
		if _, duplicate := seen[address]; duplicate {
			return
		}
		seen[address] = struct{}{}
		key := crypto.Keccak256Hash(address.Bytes())
		if _, wanted := targets[key]; !wanted {
			return
		}
		if crypto.Keccak256Hash(address.Bytes()) != key {
			fatalf("internal cryptographic verification failed for %s", address.Hex())
		}
		if err := writer.Write([]string{
			key.Hex(),
			address.Hex(),
			"canonical_protocol_source",
			"",
			"",
			fmt.Sprintf("%s:byte=%d;encoding=%s", path, offset, encoding),
		}); err != nil {
			fatalf("write match: %v", err)
		}
		delete(targets, key)
		matches++
	}

	scanFile := func(path string, info os.FileInfo) error {
		if !info.Mode().IsRegular() || info.Size() > *maxFileSize {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		filesScanned++
		bytesScanned += uint64(len(data))
		for _, match := range hexPattern.FindAllIndex(data, -1) {
			hexCandidates++
			decoded, err := hex.DecodeString(string(data[match[0]+2 : match[1]]))
			if err != nil || len(decoded) != common.AddressLength {
				return fmt.Errorf("decode hex candidate at %s:%d", path, match[0])
			}
			add(common.BytesToAddress(decoded), path, "hex", match[0])
		}
		lower := bytes.ToLower(data)
		for _, match := range bech32Pattern.FindAllIndex(lower, -1) {
			bech32Candidates++
			hrp, decoded, err := bech32.DecodeAndConvert(string(lower[match[0]:match[1]]))
			if err != nil || (hrp != "one" && hrp != "tone") || len(decoded) != common.AddressLength {
				continue
			}
			add(common.BytesToAddress(decoded), path, "bech32", match[0])
		}
		return nil
	}

	for _, source := range sources {
		info, err := os.Stat(source)
		if err != nil {
			fatalf("stat source %s: %v", source, err)
		}
		if info.IsDir() {
			if err := filepath.Walk(source, func(path string, info os.FileInfo, err error) error {
				if err != nil {
					return err
				}
				return scanFile(path, info)
			}); err != nil {
				fatalf("scan source directory %s: %v", source, err)
			}
		} else if err := scanFile(source, info); err != nil {
			fatalf("scan source file %s: %v", source, err)
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
		TargetsPath:         *targetsPath,
		SourcePaths:         sources,
		OutputPath:          *outputPath,
		OutputSHA256:        hex.EncodeToString(hasher.Sum(nil)),
		InitiallyUnresolved: initiallyUnresolved,
		FilesScanned:        filesScanned,
		BytesScanned:        bytesScanned,
		HexCandidates:       hexCandidates,
		Bech32Candidates:    bech32Candidates,
		UniqueCandidates:    uint64(len(seen)),
		Matches:             matches,
		Unresolved:          uint64(len(targets)),
		ElapsedMilliseconds: time.Since(started).Milliseconds(),
	}
	summaryData, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fatalf("encode summary: %v", err)
	}
	summaryData = append(summaryData, '\n')
	if err := os.WriteFile(*summaryPath+".partial", summaryData, 0o600); err != nil {
		fatalf("write summary: %v", err)
	}
	if err := os.Rename(*summaryPath+".partial", *summaryPath); err != nil {
		fatalf("publish summary: %v", err)
	}
	if fileSHA256(*outputPath) != result.OutputSHA256 {
		fatalf("output hash changed after publication")
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary output: %v", err)
	}
}
