"""
Tests for ModuEscrow — pure Python, no network required.

Uses `algorand-python-testing` (v1.1.0) which stubs the AVM opcodes so
contracts run directly in CPython.

Testing harness notes (v1.1.0 API)
------------------------------------
- `ctx.txn.create_group(active_txn_overrides={'sender': acct})` — sets
  the active transaction's sender for calls that read `Txn.sender`.
- `ctx.any.txn.payment(**fields)` / `.asset_transfer(**fields)` — build
  gtxn stub objects to pass as grouped-transaction parameters.
- Inner transactions (itxn.AssetTransfer) inside opt_in_asset / withdraw
  are stubbed automatically by the testing harness.
- There is NO `ctx.txn.create()` / `.application_call()` context manager
  in v1.1.0; `create_group(active_txn_overrides=...)` is the canonical way
  to set Txn.sender.

Test matrix
-----------
create              ✓ initialises global state correctly

opt_in_asset        ✓ provider can opt in
                    ✗ non-provider rejected

deposit             ✓ happy path (new consumer, full MBR payment)
                    ✓ repeat deposit accumulates
                    ✗ wrong asset rejected
                    ✗ asset not sent to app account rejected
                    ✗ zero-amount deposit rejected
                    ✗ insufficient MBR for new consumer rejected

use_credit          ✓ facilitator debits consumer, credits provider_balance
                    ✗ non-facilitator rejected
                    ✗ zero credit rejected
                    ✗ insufficient credit rejected

withdraw            ✓ provider_balance zeroed, ASA transfer submitted
                    ✗ non-provider rejected
                    ✗ zero balance rejected

read-only helpers   ✓ credit_balance returns correct value
                    ✓ box_mbr returns CREDIT_BOX_MBR constant
"""

from collections.abc import Generator

import pytest
from algopy import UInt64
from algopy_testing import AlgopyTestContext, algopy_testing_context

from contracts.modu_escrow import CREDIT_BOX_MBR, ModuEscrow


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def ctx() -> Generator[AlgopyTestContext, None, None]:
    """Fresh AVM context for every test."""
    with algopy_testing_context() as context:
        yield context


@pytest.fixture()
def deployed(ctx: AlgopyTestContext):
    """Contract deployed with standard test accounts and a mock ASA.

    Returns:
        (contract, provider, facilitator, asset, price)
    """
    provider = ctx.any.account()
    facilitator = ctx.any.account()
    asset = ctx.any.asset(decimals=6)  # mock USDC-like ASA
    price = UInt64(10_000)             # 0.01 USDC per call

    contract = ModuEscrow()
    with ctx.txn.create_group(active_txn_overrides={"sender": provider}):
        contract.create(provider, facilitator, asset, price)

    return contract, provider, facilitator, asset, price


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _app_addr(ctx: AlgopyTestContext, contract: ModuEscrow):
    """Return the application account address for `contract`."""
    return ctx.ledger.get_app(contract).address


def _deposit(
    ctx: AlgopyTestContext,
    contract: ModuEscrow,
    consumer,
    asset,
    amount: int,
    mbr_amount: int | None = None,
) -> None:
    """Helper: deposit `amount` µASA into the consumer's credit box."""
    app_addr = _app_addr(ctx, contract)
    mbr = int(CREDIT_BOX_MBR) if mbr_amount is None else mbr_amount

    mbr_payment = ctx.any.txn.payment(
        sender=consumer, receiver=app_addr, amount=mbr
    )
    xfer = ctx.any.txn.asset_transfer(
        sender=consumer,
        asset_receiver=app_addr,
        xfer_asset=asset,
        asset_amount=amount,
    )
    with ctx.txn.create_group(active_txn_overrides={"sender": consumer}):
        contract.deposit(xfer, mbr_payment)


def _earn_balance(
    ctx: AlgopyTestContext,
    contract: ModuEscrow,
    consumer,
    facilitator,
    asset,
    price: UInt64,
    calls: int,
) -> None:
    """Fund consumer and simulate `calls` use_credit calls."""
    _deposit(ctx, contract, consumer, asset, int(price) * calls)
    for _ in range(calls):
        with ctx.txn.create_group(active_txn_overrides={"sender": facilitator}):
            contract.use_credit(consumer)


# ─────────────────────────────────────────────────────────────────────────────
# create
# ─────────────────────────────────────────────────────────────────────────────


class TestCreate:
    def test_initialises_state(self, ctx: AlgopyTestContext) -> None:
        provider = ctx.any.account()
        facilitator = ctx.any.account()
        asset = ctx.any.asset()
        price = UInt64(5_000)

        contract = ModuEscrow()
        with ctx.txn.create_group(active_txn_overrides={"sender": provider}):
            contract.create(provider, facilitator, asset, price)

        assert contract.provider == provider
        assert contract.facilitator == facilitator
        assert contract.asset_id == asset.id
        assert contract.price == price
        assert contract.provider_balance == UInt64(0)


# ─────────────────────────────────────────────────────────────────────────────
# opt_in_asset
# ─────────────────────────────────────────────────────────────────────────────


class TestOptInAsset:
    def test_provider_can_opt_in(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, provider, _, _, _ = deployed
        # Should not raise
        with ctx.txn.create_group(active_txn_overrides={"sender": provider}):
            contract.opt_in_asset()

    def test_non_provider_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, _, _ = deployed
        stranger = ctx.any.account()
        with pytest.raises(Exception, match="not provider"):
            with ctx.txn.create_group(active_txn_overrides={"sender": stranger}):
                contract.opt_in_asset()


# ─────────────────────────────────────────────────────────────────────────────
# deposit
# ─────────────────────────────────────────────────────────────────────────────


class TestDeposit:
    def test_happy_path_first_deposit(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        _deposit(ctx, contract, consumer, asset, 5_000_000)
        assert contract.credit_balance(consumer) == UInt64(5_000_000)

    def test_repeat_deposit_accumulates(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        _deposit(ctx, contract, consumer, asset, 1_000_000)                  # first: MBR funded
        _deposit(ctx, contract, consumer, asset, 2_000_000, mbr_amount=0)    # repeat: no MBR
        assert contract.credit_balance(consumer) == UInt64(3_000_000)

    def test_wrong_asset_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, _, _ = deployed
        consumer = ctx.any.account()
        wrong_asset = ctx.any.asset()
        app_addr = _app_addr(ctx, contract)

        xfer = ctx.any.txn.asset_transfer(
            sender=consumer,
            asset_receiver=app_addr,
            xfer_asset=wrong_asset,
            asset_amount=1_000_000,
        )
        mbr = ctx.any.txn.payment(
            sender=consumer, receiver=app_addr, amount=int(CREDIT_BOX_MBR)
        )
        with pytest.raises(Exception, match="wrong asset"):
            with ctx.txn.create_group(active_txn_overrides={"sender": consumer}):
                contract.deposit(xfer, mbr)

    def test_wrong_receiver_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        third_party = ctx.any.account()
        app_addr = _app_addr(ctx, contract)

        xfer = ctx.any.txn.asset_transfer(
            sender=consumer,
            asset_receiver=third_party,   # wrong receiver
            xfer_asset=asset,
            asset_amount=1_000_000,
        )
        mbr = ctx.any.txn.payment(
            sender=consumer, receiver=app_addr, amount=int(CREDIT_BOX_MBR)
        )
        with pytest.raises(Exception, match="payment must be sent to the app account"):
            with ctx.txn.create_group(active_txn_overrides={"sender": consumer}):
                contract.deposit(xfer, mbr)

    def test_zero_amount_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        app_addr = _app_addr(ctx, contract)

        xfer = ctx.any.txn.asset_transfer(
            sender=consumer,
            asset_receiver=app_addr,
            xfer_asset=asset,
            asset_amount=0,   # zero
        )
        mbr = ctx.any.txn.payment(
            sender=consumer, receiver=app_addr, amount=int(CREDIT_BOX_MBR)
        )
        with pytest.raises(Exception, match="deposit must be positive"):
            with ctx.txn.create_group(active_txn_overrides={"sender": consumer}):
                contract.deposit(xfer, mbr)

    def test_insufficient_mbr_for_new_consumer_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        app_addr = _app_addr(ctx, contract)

        xfer = ctx.any.txn.asset_transfer(
            sender=consumer,
            asset_receiver=app_addr,
            xfer_asset=asset,
            asset_amount=1_000_000,
        )
        mbr = ctx.any.txn.payment(
            sender=consumer, receiver=app_addr, amount=1   # far too small
        )
        with pytest.raises(Exception, match="insufficient MBR"):
            with ctx.txn.create_group(active_txn_overrides={"sender": consumer}):
                contract.deposit(xfer, mbr)


# ─────────────────────────────────────────────────────────────────────────────
# use_credit
# ─────────────────────────────────────────────────────────────────────────────


class TestUseCredit:
    def test_happy_path(self, ctx: AlgopyTestContext, deployed) -> None:
        contract, _, facilitator, asset, price = deployed
        consumer = ctx.any.account()
        _deposit(ctx, contract, consumer, asset, int(price) * 3)

        with ctx.txn.create_group(active_txn_overrides={"sender": facilitator}):
            contract.use_credit(consumer)

        assert contract.credit_balance(consumer) == UInt64(int(price) * 2)
        assert contract.provider_balance == price

    def test_multiple_calls_accumulate_provider_balance(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, facilitator, asset, price = deployed
        consumer = ctx.any.account()
        calls = 5
        _deposit(ctx, contract, consumer, asset, int(price) * calls)

        for _ in range(calls):
            with ctx.txn.create_group(active_txn_overrides={"sender": facilitator}):
                contract.use_credit(consumer)

        assert contract.credit_balance(consumer) == UInt64(0)
        assert contract.provider_balance == UInt64(int(price) * calls)

    def test_non_facilitator_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, price = deployed
        consumer = ctx.any.account()
        _deposit(ctx, contract, consumer, asset, int(price))

        stranger = ctx.any.account()
        with pytest.raises(Exception, match="not facilitator"):
            with ctx.txn.create_group(active_txn_overrides={"sender": stranger}):
                contract.use_credit(consumer)

    def test_zero_credit_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, facilitator, _, _ = deployed
        penniless = ctx.any.account()  # never deposited

        with pytest.raises(Exception, match="insufficient credit"):
            with ctx.txn.create_group(active_txn_overrides={"sender": facilitator}):
                contract.use_credit(penniless)

    def test_insufficient_credit_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, facilitator, asset, price = deployed
        consumer = ctx.any.account()
        # Fund with price - 1 (one µASA short)
        _deposit(ctx, contract, consumer, asset, int(price) - 1)

        with pytest.raises(Exception, match="insufficient credit"):
            with ctx.txn.create_group(active_txn_overrides={"sender": facilitator}):
                contract.use_credit(consumer)


# ─────────────────────────────────────────────────────────────────────────────
# withdraw
# ─────────────────────────────────────────────────────────────────────────────


class TestWithdraw:
    def test_happy_path(self, ctx: AlgopyTestContext, deployed) -> None:
        contract, provider, facilitator, asset, price = deployed
        consumer = ctx.any.account()
        _earn_balance(ctx, contract, consumer, facilitator, asset, price, calls=5)

        with ctx.txn.create_group(active_txn_overrides={"sender": provider}):
            contract.withdraw()

        # provider_balance reset to zero; inner ASA transfer stubbed by harness
        assert contract.provider_balance == UInt64(0)

    def test_non_provider_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, provider, facilitator, asset, price = deployed
        consumer = ctx.any.account()
        _earn_balance(ctx, contract, consumer, facilitator, asset, price, calls=1)

        stranger = ctx.any.account()
        with pytest.raises(Exception, match="not provider"):
            with ctx.txn.create_group(active_txn_overrides={"sender": stranger}):
                contract.withdraw()

    def test_zero_balance_rejected(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, provider, _, _, _ = deployed
        with pytest.raises(Exception, match="nothing to withdraw"):
            with ctx.txn.create_group(active_txn_overrides={"sender": provider}):
                contract.withdraw()


# ─────────────────────────────────────────────────────────────────────────────
# Read-only helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestReadOnlyHelpers:
    def test_credit_balance_zero_for_unknown(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, _, _ = deployed
        stranger = ctx.any.account()
        assert contract.credit_balance(stranger) == UInt64(0)

    def test_credit_balance_after_deposit(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, asset, _ = deployed
        consumer = ctx.any.account()
        _deposit(ctx, contract, consumer, asset, 7_500_000)
        assert contract.credit_balance(consumer) == UInt64(7_500_000)

    def test_box_mbr_returns_constant(
        self, ctx: AlgopyTestContext, deployed
    ) -> None:
        contract, _, _, _, _ = deployed
        assert contract.box_mbr() == CREDIT_BOX_MBR
