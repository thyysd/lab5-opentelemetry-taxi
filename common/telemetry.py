from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter as GrpcLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http._log_exporter import (
    OTLPLogExporter as HttpLogExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpSpanExporter,
)
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


class TraceContextConsoleFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        span_ctx = trace.get_current_span().get_span_context()
        if span_ctx and span_ctx.is_valid:
            trace_id = f"{span_ctx.trace_id:032x}"
            span_id = f"{span_ctx.span_id:016x}"
        else:
            trace_id = "0" * 32
            span_id = "0" * 16

        base_message = super().format(record)
        return f"{base_message} [trace_id={trace_id} span_id={span_id}]"


@dataclass
class TelemetryRuntime:

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider

    def shutdown(self) -> None:
        self.logger_provider.shutdown()
        self.meter_provider.shutdown()
        self.tracer_provider.shutdown()


def _create_exporters(protocol: str, endpoint: str):
    normalized_protocol = protocol.strip().lower()
    clean_endpoint = endpoint.rstrip("/")

    if normalized_protocol in ("http", "http/protobuf"):
        span_exporter = HttpSpanExporter(endpoint=f"{clean_endpoint}/v1/traces")
        metric_exporter = HttpMetricExporter(endpoint=f"{clean_endpoint}/v1/metrics")
        log_exporter = HttpLogExporter(endpoint=f"{clean_endpoint}/v1/logs")
        return span_exporter, metric_exporter, log_exporter

    span_exporter = GrpcSpanExporter(endpoint=clean_endpoint, insecure=True)
    metric_exporter = GrpcMetricExporter(endpoint=clean_endpoint, insecure=True)
    log_exporter = GrpcLogExporter(endpoint=clean_endpoint, insecure=True)
    return span_exporter, metric_exporter, log_exporter


def setup_telemetry(service_name: str, service_version: str = "1.0.0") -> TelemetryRuntime:
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    default_endpoint = (
        "http://localhost:4318"
        if protocol.strip().lower() in ("http", "http/protobuf")
        else "http://localhost:4317"
    )
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", default_endpoint)
    metric_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000"))

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "local"),
        }
    )

    set_global_textmap(TraceContextTextMapPropagator())

    span_exporter, metric_exporter, log_exporter = _create_exporters(protocol, endpoint)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=metric_interval_ms,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    _logs.set_logger_provider(logger_provider)

    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        TraceContextConsoleFormatter(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(otel_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.getLogger(service_name).info(
        "OpenTelemetry initialized for service %s via %s (%s)",
        service_name,
        protocol,
        endpoint,
        extra={
            "otel.protocol": protocol,
            "otel.endpoint": endpoint,
            "otel.metric_interval_ms": metric_interval_ms,
        },
    )

    return TelemetryRuntime(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )