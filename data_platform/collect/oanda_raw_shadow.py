"""OANDA pricing-stream adapter for the disabled-by-default raw shadow ledger."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from data_platform.contracts.raw_event import RawEvent
from data_platform.raw.shadow import RawLedgerShadowWriter, ShadowCaptureResult


class OandaRawShadowHooks:
    """Translate one already-preserved stream line into RawEvent metadata."""

    def __init__(
        self,
        writer: RawLedgerShadowWriter,
        *,
        environment: str,
        account_id: str,
        connection_id: str,
        source_endpoint_class: str,
        collection_mode: str,
        received_at: datetime,
    ) -> None:
        self.writer = writer
        self.environment = environment
        self.account_id_sha256 = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        self.connection_id = connection_id
        self.source_endpoint_class = source_endpoint_class
        self.collection_mode = collection_mode
        self.received_at = self._required_aware(received_at, field_name="received_at")

    def capture(
        self,
        raw_payload: bytes,
        *,
        raw_sha256: str,
        observed_at: datetime,
    ) -> ShadowCaptureResult:
        if not self.writer.enabled:
            return ShadowCaptureResult(status="disabled", event_id="disabled")
        payload = self._minimal_payload(raw_payload)
        event_type = str(payload.get("type", "unknown")).strip().lower() or "unknown"
        symbol_raw = payload.get("instrument")
        symbol = None if symbol_raw is None else str(symbol_raw).replace("_", "")
        event_time = self._aware_source_time(payload.get("time"))
        source_event_id = self._source_event_id(
            event_type=event_type,
            symbol=symbol,
            event_time=event_time,
        )
        provenance = {
            "account_id_sha256": self.account_id_sha256,
            "account_environment": self.environment,
            "collection_mode": self.collection_mode,
            "connection_id": self.connection_id,
            "source_endpoint_class": self.source_endpoint_class,
            "writer": "fx_quote_collector",
        }
        if source_event_id is not None:
            provenance["source_event_id_scope"] = (
                f"pricing_stream:{self.environment}:account:{self.account_id_sha256}:"
                "event_type:instrument:event_time:v1"
            )
        event_id = hashlib.sha256(
            "|".join(
                (
                    "oanda-pricing-stream",
                    self.connection_id,
                    self.received_at.isoformat(),
                    raw_sha256,
                )
            ).encode("utf-8")
        ).hexdigest()
        event = RawEvent(
            event_id=event_id,
            source="oanda-pricing-stream",
            source_event_id=source_event_id,
            event_type=event_type,
            symbol=symbol,
            event_time=event_time,
            publish_time=None,
            receive_time=self.received_at,
            ingest_time=max(
                self.received_at,
                self._required_aware(observed_at, field_name="observed_at"),
            ),
            payload_bytes=raw_payload,
            payload_hash=raw_sha256,
            provenance=provenance,
        )
        return self.writer.capture(event)

    def quarantine(
        self,
        event_id: str,
        *,
        reason: str,
        observed_at: datetime,
    ) -> ShadowCaptureResult:
        annotation_id = hashlib.sha256(
            (
                f"{event_id}|{reason}|"
                f"{self._required_aware(observed_at, field_name='observed_at').isoformat()}"
            ).encode()
        ).hexdigest()
        return self.writer.annotate_quarantine(
            event_id,
            annotation_id=annotation_id,
            reason=reason,
            annotated_at=observed_at,
            provenance={"validator": "data_platform.collect.raw_first.v1"},
        )

    @staticmethod
    def _minimal_payload(raw_payload: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _aware_source_time(value: object) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _source_event_id(
        *,
        event_type: str,
        symbol: str | None,
        event_time: datetime | None,
    ) -> str | None:
        """Build the stable identity OANDA omits from its pricing stream.

        OANDA's documented stream payloads carry event type and source time,
        plus an instrument for prices, but no sequence/event ID.  A delivery
        ID remains connection-local; this identity is only for classifying a
        later delivery of the same provider event as duplicate or revision.
        """

        if event_time is None:
            return None
        identity = "|".join((event_type, symbol or "", event_time.isoformat()))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _required_aware(value: datetime, *, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)
