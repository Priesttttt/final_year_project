# Optional EVM ledger backend

**Status in this delivery: implemented but not executed or benchmarked.**
`docs/RESULTS.md` was generated with the default `SimulatedLedger`. Nothing in
the default demo or test suite needs an EVM node, web3.py, a contract address or
a funded account.

The optional backend exists to show how the framework would anchor roots to a
public or permissioned chain without putting records on-chain.

## What is anchored

Only one value per anchor batch: the current RFC 6962 Merkle root, the tree size
(called `sequence` in the contract ABI) and the log timestamp. Records,
payloads, documents and proofs stay off-chain. A verifier obtains a signed tree
head and proof from the log, checks the proof, then checks that the root for that
tree size appears in the contract.

## Contract

`contracts/RootAnchor.sol`:

```solidity
struct Anchor { uint64 sequence; bytes32 root; uint64 timestamp; }
function anchor(uint64 sequence, bytes32 root, uint64 timestamp) external onlyOwner;
function count() external view returns (uint256);
function get(uint256 i) external view returns (uint64, bytes32, uint64);
event Anchored(uint256 indexed index, uint64 indexed sequence, bytes32 root, uint64 timestamp);
```

- `sequence` is the tree size, not a 1-based transaction counter; the log passes
  `b, 2b, 3b, …` (plus a final flushed size). It is not required to increase by
  exactly one.
- `timestamp` is the log's millisecond clock, not the block timestamp.
- The contract is append-only and owner-gated. It does not verify Ed25519
  signatures; auditors do that off-chain.
- There is no upgrade path, pause, withdrawal or token logic. Do not put funds
  in it.

The ABI used by `src/dimf/l3_verify/anchor.py` is kept in sync with this
contract (`_ROOT_ANCHOR_ABI`). `EvmLedger` calls `anchor`, `count` and `get`
exactly as declared.

## Prerequisites (not installed by `requirements.txt`)

```bash
# Optional Python dependency
pip install -r requirements-evm.txt   # currently web3==7.16.0
```

You also need an EVM node and a way to deploy Solidity. Foundry is the shortest
path:

```bash
# Install Foundry separately (not vendored here)
curl -L https://foundry.paradigm.xyz | bash
foundryup

anvil --port 8545
```

Anvil prints deterministic funded accounts and private keys. Use only the local
Anvil key for this demo; never a real key.

## Deploy and run

In a second terminal, from the repository root:

```bash
export RPC=http://127.0.0.1:8545
export PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80

forge create contracts/RootAnchor.sol:RootAnchor \
  --rpc-url "$RPC" \
  --private-key "$PRIVATE_KEY"
# copy the "Deployed to:" address
export DIMF_EVM_CONTRACT=0xYourDeployedAddress

export DIMF_LEDGER_BACKEND=evm
export DIMF_EVM_RPC_URL="$RPC"
export DIMF_EVM_PRIVATE_KEY="$PRIVATE_KEY"
export DIMF_ANCHOR_BATCH=2

python scripts/run_demo.py --reset --anchor-batch 2
```

Open <http://127.0.0.1:8000/> and submit a few records, or call:

```bash
curl -s http://127.0.0.1:8000/ledger/anchors | python -m json.tool
```

Expected: `stats.ledger_backend` is `evm`, `ledger_transactions` equals
`ceil(tree_size / b)`, and anchor entries get EVM transaction hashes from
`EvmLedger`. Each anchor is a real transaction receipt with status 1 (the
backend raises otherwise).

## Verification flow

1. Fetch `GET /log/head` and `GET /log/proof/{record_id}`.
2. Verify the inclusion proof independently (browser or `POST /verify`).
3. Check the signed tree head's Ed25519 signature with the log's public key.
4. For the proof's `size`, find the anchored root in the contract:
   `get(i)` for `i` in `[0, count())`, or scan `Anchored` events.
5. Confirm the contract root equals the signed head root for that tree size.

If the log shows a different root for the same size, the ledger anchor is the
external evidence of equivocation.

## Why it is optional

- Anvil/Foundry and a funded account are external dependencies.
- Transaction latency and gas costs are real, not simulated; putting them in the
  default viva could fail offline.
- The EVM path could not be executed in the delivery environment, so no EVM
  number appears in `docs/RESULTS.md`.

## Limitations and safety

- The contract owner can append arbitrary roots. This is not decentralised
  governance; it is a publication log.
- There is no access control beyond a single owner, no replay protection beyond
  the chain itself, and no validation that `sequence` is monotonic.
- `timestamp` is supplied by the caller; it is not trusted as wall-clock time.
- Gas cost was not measured. The backend sets `gas=200_000` per anchor; verify
  this is sufficient on your target chain before relying on it.
- `EvmLedger.entries()` reconstructs history by iterating `get(i)`; this is fine
  for a demo, not for a large contract history.
- Do not use this contract with real funds or as a production transparency log.
