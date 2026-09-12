package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/binary"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/big"
	"os"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb/leveldb"
	"github.com/ethereum/go-ethereum/rlp"
)

const envelopeSignature = "HmnyTgd"

type taggedEnvelope struct {
	Signature string
	Tag       string
	Raw       rlp.RawValue
}

type cxReceipt struct {
	TxHash    common.Hash
	From      common.Address
	To        *common.Address
	ShardID   uint32
	ToShardID uint32
	Amount    *big.Int
}

type cxMerkleProof struct {
	BlockNum      *big.Int
	BlockHash     common.Hash
	ShardID       uint32
	CXReceiptHash common.Hash
	ShardIDs      []uint32
	CXShardHashes []common.Hash
}

type cxReceiptsProof struct {
	Receipts     []*cxReceipt
	MerkleProof  *cxMerkleProof
	Header       rlp.RawValue
	CommitSig    []byte
	CommitBitmap []byte
}

type receiptSeen struct {
	block uint64
	count uint64
}

type summary struct {
	DBPath                     string `json:"db_path"`
	StartBlockExclusive        uint64 `json:"start_block_exclusive"`
	EndBlockInclusive          uint64 `json:"end_block_inclusive"`
	BlocksScanned              uint64 `json:"blocks_scanned"`
	BlocksWithIncomingReceipts uint64 `json:"blocks_with_incoming_receipts"`
	ProofApplications          uint64 `json:"proof_applications"`
	ReceiptApplications        uint64 `json:"receipt_applications"`
	UniqueReceiptHashes        uint64 `json:"unique_receipt_hashes"`
	DuplicateReceiptApps       uint64 `json:"duplicate_receipt_applications"`
	MismatchedProofs           uint64 `json:"mismatched_proofs"`
	MismatchedReceiptApps      uint64 `json:"mismatched_receipt_applications"`
	IncomingAmountAtto         string `json:"incoming_amount_atto"`
	DuplicateAmountAtto        string `json:"duplicate_amount_atto"`
	MismatchedAmountAtto       string `json:"mismatched_amount_atto"`
	OutputPath                 string `json:"output_path"`
	OutputSHA256               string `json:"output_sha256"`
	ElapsedMilliseconds        int64  `json:"elapsed_milliseconds"`
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "fatal: "+format+"\n", args...)
	os.Exit(1)
}

func canonicalHashKey(number uint64) []byte {
	key := make([]byte, 1+8+1)
	key[0] = 'h'
	binary.BigEndian.PutUint64(key[1:9], number)
	key[9] = 'n'
	return key
}

func bodyKey(number uint64, hash common.Hash) []byte {
	key := make([]byte, 1+8+common.HashLength)
	key[0] = 'b'
	binary.BigEndian.PutUint64(key[1:9], number)
	copy(key[9:], hash[:])
	return key
}

func decodeIncomingReceipts(body []byte, number uint64) []*cxReceiptsProof {
	var envelope taggedEnvelope
	if err := rlp.DecodeBytes(body, &envelope); err != nil {
		var legacyFields []rlp.RawValue
		if legacyErr := rlp.DecodeBytes(body, &legacyFields); legacyErr == nil && len(legacyFields) == 2 {
			return nil
		}
		fatalf("decode body envelope at block %d: %v", number, err)
	}
	if envelope.Signature != envelopeSignature {
		return nil
	}
	var fields []rlp.RawValue
	if err := rlp.DecodeBytes(envelope.Raw, &fields); err != nil {
		fatalf("decode body fields at block %d: %v", number, err)
	}
	var incomingIndex int
	switch envelope.Tag {
	case "v1":
		incomingIndex = 2
	case "v2":
		incomingIndex = 3
	default:
		fatalf("unsupported body tag at block %d: %q", number, envelope.Tag)
	}
	if len(fields) <= incomingIndex {
		fatalf("short %s body at block %d: %d fields", envelope.Tag, number, len(fields))
	}
	var proofs []*cxReceiptsProof
	if err := rlp.DecodeBytes(fields[incomingIndex], &proofs); err != nil {
		fatalf("decode incoming receipts at block %d: %v", number, err)
	}
	return proofs
}

func decodeSignedHeader(raw rlp.RawValue, destinationBlock uint64, proofIndex int) (common.Hash, uint32, *big.Int) {
	var envelope taggedEnvelope
	if err := rlp.DecodeBytes(raw, &envelope); err != nil {
		fatalf("decode source header envelope at destination block %d proof %d: %v", destinationBlock, proofIndex, err)
	}
	if envelope.Signature != envelopeSignature {
		fatalf("invalid source header envelope signature at destination block %d proof %d: %q", destinationBlock, proofIndex, envelope.Signature)
	}
	var fields []rlp.RawValue
	if err := rlp.DecodeBytes(envelope.Raw, &fields); err != nil {
		fatalf("decode source header fields at destination block %d proof %d: %v", destinationBlock, proofIndex, err)
	}
	if len(fields) <= 16 {
		fatalf("short source header at destination block %d proof %d: %d fields", destinationBlock, proofIndex, len(fields))
	}
	number := new(big.Int)
	if err := rlp.DecodeBytes(fields[8], number); err != nil {
		fatalf("decode source header number at destination block %d proof %d: %v", destinationBlock, proofIndex, err)
	}
	var shardID uint32
	if err := rlp.DecodeBytes(fields[16], &shardID); err != nil {
		fatalf("decode source header shard at destination block %d proof %d: %v", destinationBlock, proofIndex, err)
	}
	return crypto.Keccak256Hash(raw), shardID, number
}

func main() {
	var (
		dbPath  = flag.String("db", "", "path to shard-0 archive LevelDB")
		start   = flag.Uint64("start", 0, "old checkpoint block, excluded")
		end     = flag.Uint64("end", 0, "new checkpoint block, included")
		output  = flag.String("output", "", "CSV output path")
		cacheMB = flag.Int("cache-mb", 4096, "LevelDB cache in MiB")
		handles = flag.Int("handles", 4096, "LevelDB open-file handles")
	)
	flag.Parse()
	if *dbPath == "" || *output == "" || *end <= *start {
		flag.Usage()
		os.Exit(2)
	}

	disk, err := leveldb.New(*dbPath, *cacheMB, *handles, "", true)
	if err != nil {
		fatalf("open database read-only: %v", err)
	}
	defer disk.Close()

	partial := *output + ".partial"
	outputFile, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		fatalf("create output: %v", err)
	}
	outputComplete := false
	defer func() {
		outputFile.Close()
		if !outputComplete {
			os.Remove(partial)
		}
	}()
	hasher := sha256.New()
	buffered := bufio.NewWriterSize(io.MultiWriter(outputFile, hasher), 1024*1024)
	writer := csv.NewWriter(buffered)
	if err := writer.Write([]string{
		"destination_block",
		"proof_index",
		"receipt_index",
		"tx_hash",
		"from",
		"to",
		"amount_atto",
		"signed_source_header_hash",
		"signed_source_shard",
		"signed_source_block",
		"claimed_source_block_hash",
		"claimed_source_shard",
		"claimed_source_block",
		"proof_identity_mismatch",
		"duplicate_receipt_application",
		"first_application_block",
		"application_number",
	}); err != nil {
		fatalf("write CSV header: %v", err)
	}

	started := time.Now()
	lastProgress := started
	seen := make(map[common.Hash]receiptSeen)
	incomingAmount := new(big.Int)
	duplicateAmount := new(big.Int)
	mismatchedAmount := new(big.Int)
	var blocksWithIncoming, proofApplications, receiptApplications uint64
	var duplicateApplications, mismatchedProofs, mismatchedApplications uint64

	for number := *start + 1; number <= *end; number++ {
		hashBytes, err := disk.Get(canonicalHashKey(number))
		if err != nil || len(hashBytes) != common.HashLength {
			fatalf("missing canonical hash at block %d", number)
		}
		hash := common.BytesToHash(hashBytes)
		body, err := disk.Get(bodyKey(number, hash))
		if err != nil || len(body) == 0 {
			fatalf("missing canonical body at block %d hash %s", number, hash)
		}
		proofs := decodeIncomingReceipts(body, number)
		if len(proofs) != 0 {
			blocksWithIncoming++
		}
		for proofIndex, proof := range proofs {
			proofApplications++
			if proof == nil || len(proof.Header) == 0 || proof.MerkleProof == nil {
				fatalf("incomplete proof at destination block %d index %d", number, proofIndex)
			}
			signedHash, signedShard, signedNumber := decodeSignedHeader(proof.Header, number, proofIndex)
			claimedNumber := proof.MerkleProof.BlockNum
			if signedNumber == nil || claimedNumber == nil {
				fatalf("nil source block number at destination block %d index %d", number, proofIndex)
			}
			mismatch := signedHash != proof.MerkleProof.BlockHash ||
				signedShard != proof.MerkleProof.ShardID ||
				signedNumber.Cmp(claimedNumber) != 0
			if mismatch {
				mismatchedProofs++
			}
			for receiptIndex, receipt := range proof.Receipts {
				if receipt == nil || receipt.To == nil || receipt.Amount == nil || receipt.Amount.Sign() < 0 {
					fatalf("invalid receipt at destination block %d proof %d receipt %d", number, proofIndex, receiptIndex)
				}
				receiptApplications++
				incomingAmount.Add(incomingAmount, receipt.Amount)
				previous := seen[receipt.TxHash]
				duplicate := previous.count != 0
				if duplicate {
					duplicateApplications++
					duplicateAmount.Add(duplicateAmount, receipt.Amount)
				} else {
					previous.block = number
				}
				previous.count++
				seen[receipt.TxHash] = previous
				if mismatch {
					mismatchedApplications++
					mismatchedAmount.Add(mismatchedAmount, receipt.Amount)
				}
				if err := writer.Write([]string{
					fmt.Sprintf("%d", number),
					fmt.Sprintf("%d", proofIndex),
					fmt.Sprintf("%d", receiptIndex),
					receipt.TxHash.Hex(),
					receipt.From.Hex(),
					receipt.To.Hex(),
					receipt.Amount.String(),
					signedHash.Hex(),
					fmt.Sprintf("%d", signedShard),
					signedNumber.String(),
					proof.MerkleProof.BlockHash.Hex(),
					fmt.Sprintf("%d", proof.MerkleProof.ShardID),
					claimedNumber.String(),
					fmt.Sprintf("%t", mismatch),
					fmt.Sprintf("%t", duplicate),
					fmt.Sprintf("%d", previous.block),
					fmt.Sprintf("%d", previous.count),
				}); err != nil {
					fatalf("write receipt application: %v", err)
				}
			}
		}
		if time.Since(lastProgress) >= 10*time.Second {
			fmt.Fprintf(
				os.Stderr,
				"progress block=%d/%d blocks=%d incoming_blocks=%d proofs=%d receipts=%d mismatched=%d duplicates=%d rate=%.0f_blocks/s\n",
				number,
				*end,
				number-*start,
				blocksWithIncoming,
				proofApplications,
				receiptApplications,
				mismatchedProofs,
				duplicateApplications,
				float64(number-*start)/time.Since(started).Seconds(),
			)
			lastProgress = time.Now()
		}
	}

	writer.Flush()
	if err := writer.Error(); err != nil {
		fatalf("flush CSV: %v", err)
	}
	if err := buffered.Flush(); err != nil {
		fatalf("flush output: %v", err)
	}
	if err := outputFile.Sync(); err != nil {
		fatalf("sync output: %v", err)
	}
	if err := outputFile.Close(); err != nil {
		fatalf("close output: %v", err)
	}
	if err := os.Rename(partial, *output); err != nil {
		fatalf("publish output: %v", err)
	}
	outputComplete = true

	result := summary{
		DBPath:                     *dbPath,
		StartBlockExclusive:        *start,
		EndBlockInclusive:          *end,
		BlocksScanned:              *end - *start,
		BlocksWithIncomingReceipts: blocksWithIncoming,
		ProofApplications:          proofApplications,
		ReceiptApplications:        receiptApplications,
		UniqueReceiptHashes:        uint64(len(seen)),
		DuplicateReceiptApps:       duplicateApplications,
		MismatchedProofs:           mismatchedProofs,
		MismatchedReceiptApps:      mismatchedApplications,
		IncomingAmountAtto:         incomingAmount.String(),
		DuplicateAmountAtto:        duplicateAmount.String(),
		MismatchedAmountAtto:       mismatchedAmount.String(),
		OutputPath:                 *output,
		OutputSHA256:               hex.EncodeToString(hasher.Sum(nil)),
		ElapsedMilliseconds:        time.Since(started).Milliseconds(),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fatalf("encode summary: %v", err)
	}
}
