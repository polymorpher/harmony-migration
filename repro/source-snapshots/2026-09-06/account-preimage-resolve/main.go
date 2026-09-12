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
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
)

type preimageResolver struct {
	iterator ethdb.Iterator
	valid    bool
	key      common.Hash
	value    []byte
}

type databasePaths []string

func (paths *databasePaths) String() string {
	return strings.Join(*paths, ",")
}

func (paths *databasePaths) Set(value string) error {
	*paths = append(*paths, value)
	return nil
}

type summary struct {
	InputPath        string   `json:"input_path"`
	OutputPath       string   `json:"output_path"`
	PreimageDBs      []string `json:"preimage_dbs"`
	OutputSHA256     string   `json:"output_sha256"`
	Rows             uint64   `json:"rows"`
	InitiallyMissing uint64   `json:"initially_missing"`
	Resolved         uint64   `json:"resolved"`
	Unresolved       uint64   `json:"unresolved"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func parseSecureKey(value string) common.Hash {
	raw := strings.TrimPrefix(value, "0x")
	decoded, err := hex.DecodeString(raw)
	if err != nil || len(decoded) != common.HashLength {
		fatalf("invalid secure key %q", value)
	}
	return common.BytesToHash(decoded)
}

func verifyAddress(key common.Hash, address string) {
	if !common.IsHexAddress(address) {
		fatalf("invalid address %q for secure key %s", address, key.Hex())
	}
	addr := common.HexToAddress(address)
	if got := crypto.Keccak256Hash(addr.Bytes()); got != key {
		fatalf("address %s does not match secure key %s", address, key.Hex())
	}
}

func newPreimageResolver(db ethdb.Database) *preimageResolver {
	resolver := &preimageResolver{
		iterator: db.NewIterator(rawdb.PreimagePrefix, nil),
	}
	resolver.advance()
	return resolver
}

func (resolver *preimageResolver) advance() {
	resolver.valid = false
	for resolver.iterator.Next() {
		key := resolver.iterator.Key()
		if len(key) != len(rawdb.PreimagePrefix)+common.HashLength {
			continue
		}
		resolver.key = common.BytesToHash(key[len(rawdb.PreimagePrefix):])
		resolver.value = common.CopyBytes(resolver.iterator.Value())
		resolver.valid = true
		return
	}
}

func (resolver *preimageResolver) resolve(target common.Hash) []byte {
	for resolver.valid && bytes.Compare(resolver.key[:], target[:]) < 0 {
		resolver.advance()
	}
	if resolver.valid && resolver.key == target {
		return resolver.value
	}
	return nil
}

func main() {
	var paths databasePaths
	var (
		inputPath  = flag.String("input", "", "positive-balance CSV to resolve")
		outputPath = flag.String("output", "", "resolved CSV output path")
		cacheMB    = flag.Int("cache-mb", 64, "cache per preimage database in MiB")
		handles    = flag.Int("handles", 64, "open-file handles per preimage database")
	)
	flag.Var(&paths, "preimage-db", "fallback LevelDB containing secure-key preimages; repeatable")
	flag.Parse()

	if *inputPath == "" || *outputPath == "" || len(paths) == 0 {
		flag.Usage()
		os.Exit(2)
	}
	if *inputPath == *outputPath {
		fatalf("input and output paths must differ")
	}

	databases := make([]ethdb.Database, 0, len(paths))
	resolvers := make([]*preimageResolver, 0, len(paths))
	for _, path := range paths {
		disk, err := leveldb.New(path, *cacheMB, *handles, "", true)
		if err != nil {
			fatalf("open preimage database %s read-only: %v", path, err)
		}
		db := rawdb.NewDatabase(disk)
		databases = append(databases, db)
		defer db.Close()
		resolver := newPreimageResolver(db)
		resolvers = append(resolvers, resolver)
		defer resolver.iterator.Release()
	}

	input, err := os.Open(*inputPath)
	if err != nil {
		fatalf("open input: %v", err)
	}
	defer input.Close()
	reader := csv.NewReader(bufio.NewReaderSize(input, 1024*1024))
	header, err := reader.Read()
	if err != nil {
		fatalf("read CSV header: %v", err)
	}

	columns := make(map[string]int, len(header))
	for i, name := range header {
		columns[name] = i
	}
	keyColumn, hasKey := columns["secure_key"]
	addressColumn, hasAddress := columns["address"]
	if !hasKey || !hasAddress {
		fatalf("CSV must contain secure_key and address columns")
	}

	partialPath := *outputPath + ".partial"
	output, err := os.OpenFile(partialPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create partial output: %v", err)
	}
	outputComplete := false
	defer func() {
		output.Close()
		if !outputComplete {
			os.Remove(partialPath)
		}
	}()

	hasher := sha256.New()
	bufferedOutput := bufio.NewWriterSize(io.MultiWriter(output, hasher), 1024*1024)
	writer := csv.NewWriter(bufferedOutput)
	if err := writer.Write(header); err != nil {
		fatalf("write CSV header: %v", err)
	}

	var (
		rows, initiallyMissing, resolved, unresolved uint64
		previousKey                                  common.Hash
		havePreviousKey                              bool
	)
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fatalf("read CSV row %d: %v", rows+2, err)
		}
		if len(row) != len(header) {
			fatalf("CSV row %d has %d fields, expected %d", rows+2, len(row), len(header))
		}
		rows++
		key := parseSecureKey(row[keyColumn])
		if havePreviousKey && bytes.Compare(previousKey[:], key[:]) >= 0 {
			fatalf("CSV secure keys are not strictly increasing at row %d", rows+1)
		}
		previousKey = key
		havePreviousKey = true
		if row[addressColumn] != "" {
			verifyAddress(key, row[addressColumn])
		} else {
			initiallyMissing++
			for _, resolver := range resolvers {
				if preimage := resolver.resolve(key); len(preimage) != 0 {
					if len(preimage) != common.AddressLength {
						fatalf("invalid preimage length %d for secure key %s", len(preimage), key.Hex())
					}
					address := common.BytesToAddress(preimage).Hex()
					verifyAddress(key, address)
					row[addressColumn] = address
					resolved++
					break
				}
			}
			if row[addressColumn] == "" {
				unresolved++
			}
		}
		if err := writer.Write(row); err != nil {
			fatalf("write CSV row: %v", err)
		}
	}
	for i, resolver := range resolvers {
		if err := resolver.iterator.Error(); err != nil {
			fatalf("preimage iterator for %s: %v", paths[i], err)
		}
	}

	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := bufferedOutput.Flush(); err != nil {
		fatalf("flush output buffer: %v", err)
	}
	if err := output.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := output.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partialPath, *outputPath); err != nil {
		fatalf("publish output: %v", err)
	}
	outputComplete = true

	result := summary{
		InputPath:        *inputPath,
		OutputPath:       *outputPath,
		PreimageDBs:      paths,
		OutputSHA256:     hex.EncodeToString(hasher.Sum(nil)),
		Rows:             rows,
		InitiallyMissing: initiallyMissing,
		Resolved:         resolved,
		Unresolved:       unresolved,
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
	if unresolved != 0 {
		os.Exit(3)
	}
}
