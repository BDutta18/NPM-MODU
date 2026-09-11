"""
deploy_testnet.py — Deploy ModuEscrow to Algorand TestNet.

Uses plain py-algorand-sdk + AtomicTransactionComposer.  No algokit_utils
typed client or codegen output required — just compiled TEAL files.

Compile first
-------------
  python -m puya contracts/modu_escrow.py --out-dir artifacts/
    OR
  algokit compile python contracts/modu_escrow.py --out-dir artifacts/

Both produce:
  artifacts/ModuEscrow.approval.teal
  artifacts/ModuEscrow.clear.teal

Architecture note: create="require"
-------------------------------------
The AlgoPy `@arc4.abimethod(create="require")` decorator means the ABI
`create()` method MUST be the call that creates the application — it cannot
be called as a follow-up NoOp after a bare ApplicationCreateTxn.

This script handles that by using AtomicTransactionComposer.add_method_call
with on_complete=OnComplete.NoOpOC and extra_pages/schema set so the SDK
produces a single ApplicationCreateTxn that carries the ABI method selector
and arguments in app_args.  One transaction, all state initialised atomically.

Stages
------
  Stage 1  Deploy + initialise — single ApplicationCreateTxn with ABI args
           (provider, facilitator, asset_id, price encoded as ABI payload).
  Stage 2  Fund app MBR — PaymentTxn to the app account (covers base MBR
           + headroom for credit boxes consumers will create).
  Stage 3  Opt into ASA — ApplicationNoOpTxn calling opt_in_asset() with
           fee = 2 × min_fee to cover the inner zero-amount ASA self-transfer.

Idempotency
-----------
  Set APP_ID to re-run Stages 2-3 against an existing app (useful if a prior
  run was interrupted after Stage 1).

Env vars
--------
Required:
  DEPLOYER_MNEMONIC       25-word mnemonic, funded TestNet account.
                          Get test ALGO: https://bank.testnet.algorand.network

Optional:
  PROVIDER_MNEMONIC       Provider wallet. Defaults to deployer wallet.
  FACILITATOR_ADDRESS     modu proxy address. Defaults to deployer address.
  ASSET_ID                Existing TestNet ASA id. Creates mock USDC if unset.
  PRICE                   µASA per API call. Default: 10000 (= 0.01 USDC).
  APP_ID                  Skip Stage 1 if re-running against an existing app.
  APPROVAL_TEAL_PATH      Default: artifacts/ModuEscrow.approval.teal
  CLEAR_TEAL_PATH         Default: artifacts/ModuEscrow.clear.teal

Run
---
  pip install py-algorand-sdk
  export DEPLOYER_MNEMONIC="word1 word2 ... word25"
  python scripts/deploy_testnet.py
"""

from __future__ import annotations

import base64
import os
import sys
import time
from typing import Any

from algosdk import account as algo_account
from algosdk import mnemonic as algo_mnemonic
from algosdk import transaction
from algosdk.abi import Argument, Method, Returns
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.v2client import algod

# ─── configuration ────────────────────────────────────────────────────────────

ALGOD_ADDRESS = "https://testnet-api.algonode.cloud"
ALGOD_TOKEN = ""  # algonode public endpoint — no token needed

_DEFAULT_APPROVAL = os.getenv("APPROVAL_TEAL_PATH", "")
_DEFAULT_CLEAR = os.getenv("CLEAR_TEAL_PATH", "")


def _find_teal(artifacts_dir: str = "artifacts") -> tuple[str, str]:
    """Auto-detect approval and clear TEAL files in artifacts/.

    puyapy names files after the class: ``ModuEscrow.approval.teal``
    algokit compile may use the module name: ``modu_escrow.approval.teal``
    Prefer env-var overrides, then scan the directory.
    """
    if _DEFAULT_APPROVAL and _DEFAULT_CLEAR:
        return _DEFAULT_APPROVAL, _DEFAULT_CLEAR

    if not os.path.isdir(artifacts_dir):
        sys.exit(
            f"\n[ERROR] artifacts/ directory not found.\n"
            "Compile first:\n"
            "  python -m pipx run puya contracts/modu_escrow.py --out-dir artifacts/\n"
            "or:\n"
            "  algokit compile python contracts/modu_escrow.py --out-dir artifacts/"
        )

    import glob
    approval_files = glob.glob(f"{artifacts_dir}/*.approval.teal")
    clear_files = glob.glob(f"{artifacts_dir}/*.clear.teal")

    if not approval_files or not clear_files:
        sys.exit(
            f"\n[ERROR] No compiled TEAL files found in {artifacts_dir}/\n"
            "Compile first:\n"
            "  python -m pipx run puya contracts/modu_escrow.py --out-dir artifacts/"
        )

    return approval_files[0], clear_files[0]
DEFAULT_PRICE = int(os.getenv("PRICE", "10000"))  # 0.01 USDC (6 decimals)

# Global schema: provider (bytes), facilitator (bytes), asset_id (uint),
# price (uint), provider_balance (uint) — use generous headroom.
GLOBAL_SCHEMA = transaction.StateSchema(num_uints=8, num_byte_slices=4)
LOCAL_SCHEMA = transaction.StateSchema(num_uints=0, num_byte_slices=0)

# ALGO sent to the app account on deployment to cover:
#   - base app MBR           : 100 000 µALGO
#   - per global key (uints) :  28 500 µALGO  (8 × ~3 562)
#   - per global key (bytes) :  28 500 µALGO  (4 × ~7 125)
#   - headroom for boxes     : 143 000 µALGO
APP_MBR_FUND = 300_000  # 0.3 ALGO


# ─── ABI method descriptors ───────────────────────────────────────────────────
# Built manually so the script works without ARC-32 codegen output.

def _method(name: str, args: list[tuple[str, str]], returns: str) -> Method:
    return Method(
        name=name,
        args=[Argument(arg_type=t, name=n) for t, n in args],
        returns=Returns(returns),
    )


CREATE_METHOD = _method(
    "create",
    [
        ("address", "provider"),
        ("address", "facilitator"),
        ("uint64", "asset"),   # puyapy 5.x emits uint64 for Asset args in the ABI signature
        ("uint64", "price"),
    ],
    "void",
)

OPT_IN_METHOD = _method("opt_in_asset", [], "void")


# ─── helpers ─────────────────────────────────────────────────────────────────


def get_client() -> algod.AlgodClient:
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_ADDRESS)


def compile_teal(client: algod.AlgodClient, path: str) -> bytes:
    """Read a TEAL source file and compile it via the algod endpoint."""
    if not os.path.exists(path):
        sys.exit(
            f"\n[ERROR] TEAL file not found: {path}\n"
            "Compile first:\n"
            "  python -m puya contracts/modu_escrow.py --out-dir artifacts/\n"
            "or:\n"
            "  algokit compile python contracts/modu_escrow.py --out-dir artifacts/"
        )
    with open(path) as f:
        source = f.read()
    result = client.compile(source)
    return base64.b64decode(result["result"])


def wait(client: algod.AlgodClient, txid: str, rounds: int = 6) -> dict[str, Any]:
    return transaction.wait_for_confirmation(client, txid, rounds)


def _sp(
    client: algod.AlgodClient,
    fee_multiplier: int = 1,
    flat: bool = False,
) -> transaction.SuggestedParams:
    sp = client.suggested_params()
    if fee_multiplier > 1 or flat:
        sp.fee = sp.min_fee * fee_multiplier
        sp.flat_fee = True
    return sp


def _log(stage: str, msg: str) -> None:
    print(f"\n[{stage}] {msg}")


# ─── asset helper ─────────────────────────────────────────────────────────────


def resolve_asset(
    client: algod.AlgodClient, sender: str, private_key: str
) -> int:
    env_id = os.getenv("ASSET_ID")
    if env_id:
        asset_id = int(env_id)
        print(f"  Using existing ASA: {asset_id}")
        return asset_id

    print("  ASSET_ID not set — creating mock USDC on TestNet ...")
    txn = transaction.AssetCreateTxn(
        sender=sender,
        sp=_sp(client),
        total=1_000_000_000_000,  # 1 M USDC (6 decimals)
        decimals=6,
        default_frozen=False,
        unit_name="mUSDC",
        asset_name="Mock USDC (modu test)",
        manager=sender,
        reserve=sender,
    )
    signed = txn.sign(private_key)
    txid = client.send_transaction(signed)
    result = wait(client, txid)
    asset_id = int(result["asset-index"])
    print(f"  Created mock USDC ASA: {asset_id}")
    print(f"  Explorer: https://lora.algokit.io/testnet/asset/{asset_id}")
    return asset_id


# ─── stages ───────────────────────────────────────────────────────────────────


def stage1_create_and_init(
    client: algod.AlgodClient,
    sender: str,
    signer: AccountTransactionSigner,
    private_key: str,
    approval: bytes,
    clear: bytes,
    provider_addr: str,
    facilitator_addr: str,
    asset_id: int,
    price: int,
) -> int:
    """Deploy the app AND call create() in a single transaction.

    AtomicTransactionComposer builds an ApplicationCreateTxn that includes
    the ABI method selector + encoded args in app_args.  This is the only
    way to satisfy `create="require"` on the ABI method — the contract's
    create() must be the transaction that creates the app.

    Returns app_id.
    """
    _log("Stage 1", "Deploying + initialising (create + ABI args in one txn) ...")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,                                         # 0 = create new app
        method=CREATE_METHOD,
        sender=sender,
        sp=_sp(client),
        signer=signer,
        method_args=[provider_addr, facilitator_addr, asset_id, price],
        on_complete=transaction.OnComplete.NoOpOC,
        approval_program=approval,
        clear_program=clear,
        global_schema=GLOBAL_SCHEMA,
        local_schema=LOCAL_SCHEMA,
        foreign_assets=[],         # asset_id is encoded as uint64 arg; no foreign-asset slot needed
    )

    result = atc.execute(client, wait_rounds=6)
    # The confirmed transaction result carries the application-index
    txid = result.tx_ids[0]
    confirmed = wait(client, txid)
    app_id = int(confirmed["application-index"])
    app_address = transaction.logic.get_application_address(app_id)

    print(f"  App ID:      {app_id}")
    print(f"  App address: {app_address}")
    print(f"  Explorer:    https://lora.algokit.io/testnet/application/{app_id}")
    return app_id


def stage2_fund_mbr(
    client: algod.AlgodClient,
    app_id: int,
    sender: str,
    private_key: str,
) -> None:
    """Send ALGO to the app account to cover minimum balance requirements."""
    app_address = transaction.logic.get_application_address(app_id)
    _log("Stage 2", f"Funding app MBR ({APP_MBR_FUND / 1e6:.3f} ALGO → {app_address[:16]}...)")
    txn = transaction.PaymentTxn(
        sender=sender,
        sp=_sp(client),
        receiver=app_address,
        amt=APP_MBR_FUND,
    )
    signed = txn.sign(private_key)
    txid = client.send_transaction(signed)
    wait(client, txid)
    print("  MBR funded ✓")


def stage3_opt_in_asset(
    client: algod.AlgodClient,
    app_id: int,
    provider_addr: str,
    provider_signer: AccountTransactionSigner,
    asset_id: int,
) -> None:
    """Call opt_in_asset() with fee = 2 × min_fee to cover the inner ASA transfer."""
    _log("Stage 3", f"Opting app into ASA {asset_id} (outer fee = 2 × min_fee) ...")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=OPT_IN_METHOD,
        sender=provider_addr,
        sp=_sp(client, fee_multiplier=2),   # inner fee=0 covered by fee pool
        signer=provider_signer,
        foreign_assets=[asset_id],
    )
    atc.execute(client, wait_rounds=6)
    print("  App opted into ASA ✓")


# ─── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    print("═" * 62)
    print("  ModuEscrow — TestNet Deployment")
    print("═" * 62)

    # ── load wallets ─────────────────────────────────────────────────
    raw = os.environ.get("DEPLOYER_MNEMONIC")
    if not raw:
        sys.exit("\n[ERROR] Set DEPLOYER_MNEMONIC environment variable.\n"
                 "Get test ALGO at: https://bank.testnet.algorand.network")

    deployer_pk = algo_mnemonic.to_private_key(raw)
    deployer_addr = algo_account.address_from_private_key(deployer_pk)
    deployer_signer = AccountTransactionSigner(deployer_pk)

    provider_raw = os.getenv("PROVIDER_MNEMONIC")
    if provider_raw:
        provider_pk = algo_mnemonic.to_private_key(provider_raw)
        provider_addr = algo_account.address_from_private_key(provider_pk)
        provider_signer = AccountTransactionSigner(provider_pk)
    else:
        provider_pk, provider_addr, provider_signer = (
            deployer_pk, deployer_addr, deployer_signer
        )

    facilitator_addr = os.getenv("FACILITATOR_ADDRESS", deployer_addr)
    price = DEFAULT_PRICE

    client = get_client()

    # ── balance check ────────────────────────────────────────────────
    info = client.account_info(deployer_addr)
    balance_algo = info.get("amount", 0) / 1e6
    print(f"\nDeployer:  {deployer_addr}")
    print(f"Provider:  {provider_addr}")
    print(f"Facilitator: {facilitator_addr}")
    print(f"Balance:   {balance_algo:.3f} ALGO")
    if balance_algo < 1.0:
        sys.exit(
            "\n[ERROR] Deployer balance < 1 ALGO.\n"
            "Fund it at: https://bank.testnet.algorand.network"
        )

    # ── resolve settlement ASA ───────────────────────────────────────
    _log("Setup", "Resolving settlement ASA ...")
    asset_id = resolve_asset(client, deployer_addr, deployer_pk)

    # ── existing app shortcut ────────────────────────────────────────
    existing_app_id = os.getenv("APP_ID")
    if existing_app_id:
        app_id = int(existing_app_id)
        _log("Stage 1", f"Skipped — reusing APP_ID={app_id}")
    else:
        approval_path, clear_path = _find_teal()
        approval = compile_teal(client, approval_path)
        clear = compile_teal(client, clear_path)
        app_id = stage1_create_and_init(
            client,
            deployer_addr,
            deployer_signer,
            deployer_pk,
            approval,
            clear,
            provider_addr,
            facilitator_addr,
            asset_id,
            price,
        )

    app_address = transaction.logic.get_application_address(app_id)

    # ── fund MBR ─────────────────────────────────────────────────────
    stage2_fund_mbr(client, app_id, deployer_addr, deployer_pk)

    # ── opt in to ASA ────────────────────────────────────────────────
    stage3_opt_in_asset(client, app_id, provider_addr, provider_signer, asset_id)

    # ── summary ──────────────────────────────────────────────────────
    print("\n" + "═" * 62)
    print("  Deployment complete ✓")
    print("═" * 62)
    print(f"  App ID:         {app_id}")
    print(f"  App address:    {app_address}")
    print(f"  Provider:       {provider_addr}")
    print(f"  Facilitator:    {facilitator_addr}")
    print(f"  ASA:            {asset_id}")
    print(f"  Price per call: {price} µASA  ({price / 1e6:.6f} USDC)")
    print(f"\n  Explorer: https://lora.algokit.io/testnet/application/{app_id}")
    print()

    # ── persist deployment state ─────────────────────────────────────
    env_out = (
        f"# ModuEscrow TestNet — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        f"APP_ID={app_id}\n"
        f"APP_ADDRESS={app_address}\n"
        f"PROVIDER_ADDRESS={provider_addr}\n"
        f"FACILITATOR_ADDRESS={facilitator_addr}\n"
        f"ASSET_ID={asset_id}\n"
        f"PRICE={price}\n"
    )
    out_path = "deployed.env"
    with open(out_path, "w") as f:
        f.write(env_out)
    print(f"  Saved deployment state → {out_path}")


if __name__ == "__main__":
    main()
