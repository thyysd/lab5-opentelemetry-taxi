from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, status
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from common.telemetry import setup_telemetry

SERVICE_NAME = "order-service"
DISPATCH_SERVICE_URL = os.getenv("DISPATCH_SERVICE_URL", "http://localhost:8002")

telemetry_runtime = setup_telemetry(service_name=SERVICE_NAME)
HTTPXClientInstrumentor().instrument()

logger = logging.getLogger(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

orders_counter = meter.create_counter(
    name="taxi_orders_total",
    unit="1",
    description="Total number of taxi orders processed by order-service",
)

TARIFF_BASE_PRICES: dict[str, float] = {
    "economy": 250.0,
    "comfort": 420.0,
    "business": 780.0,
}

ZONE_MULTIPLIERS: dict[str, float] = {
    "center": 1.35,
    "north": 1.10,
    "south": 1.05,
    "airport": 1.60,
}


class OrderRequest(BaseModel):
    passenger_id: str = Field(..., min_length=1, examples=["pax-1042"])
    pickup_zone: str = Field(..., min_length=1, examples=["center"])
    tariff: Literal["economy", "comfort", "business"] = "comfort"
    simulate_slow: bool = False
    simulate_error: bool = False


class AssignedDriver(BaseModel):
    driver_id: str
    driver_name: str
    car_model: str
    rating: float
    distance_km: float
    eta_minutes: int


class OrderResponse(BaseModel):
    order_id: str
    passenger_id: str
    pickup_zone: str
    tariff: str
    estimated_price: float
    status: str
    driver: AssignedDriver


class InvalidPickupZoneError(ValueError):


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        base_url=DISPATCH_SERVICE_URL,
        timeout=httpx.Timeout(10.0, connect=2.0),
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        telemetry_runtime.shutdown()


app = FastAPI(
    title="Taxi Order Service",
    version="1.0.0",
    lifespan=lifespan,
)

FastAPIInstrumentor.instrument_app(app)


def calculate_trip_estimate(
    order_id: str,
    passenger_id: str,
    pickup_zone: str,
    tariff: str,
) -> float:
    normalized_zone = pickup_zone.strip().lower()

    with tracer.start_as_current_span(
        "calculate_trip_estimate",
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("passenger.id", passenger_id)
        span.set_attribute("taxi.tariff", tariff)
        span.set_attribute("pickup.zone", normalized_zone)

        if normalized_zone not in ZONE_MULTIPLIERS:
            exc = InvalidPickupZoneError(
                f"Unsupported pickup zone '{pickup_zone}'. "
                f"Allowed zones: {', '.join(sorted(ZONE_MULTIPLIERS))}"
            )
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise exc

        base_price = TARIFF_BASE_PRICES[tariff]
        zone_multiplier = ZONE_MULTIPLIERS[normalized_zone]
        estimated_price = round(base_price * zone_multiplier, 2)

        span.set_attribute("order.estimated_price", estimated_price)

        logger.info(
            "Trip estimate calculated for order %s",
            order_id,
            extra={
                "order.id": order_id,
                "passenger.id": passenger_id,
                "pickup.zone": normalized_zone,
                "taxi.tariff": tariff,
                "order.estimated_price": estimated_price,
            },
        )
        return estimated_price


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order(request: OrderRequest) -> OrderResponse:
    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    normalized_zone = request.pickup_zone.strip().lower()

    logger.info(
        "Received taxi order request %s from passenger %s",
        order_id,
        request.passenger_id,
        extra={
            "order.id": order_id,
            "passenger.id": request.passenger_id,
            "pickup.zone": normalized_zone,
            "taxi.tariff": request.tariff,
            "fault.simulate_slow": request.simulate_slow,
            "fault.simulate_error": request.simulate_error,
        },
    )

    try:
        estimated_price = calculate_trip_estimate(
            order_id=order_id,
            passenger_id=request.passenger_id,
            pickup_zone=normalized_zone,
            tariff=request.tariff,
        )
    except InvalidPickupZoneError as exc:
        orders_counter.add(
            1,
            attributes={
                "tariff": request.tariff,
                "pickup_zone": normalized_zone,
                "status": "error",
            },
        )
        logger.warning(
            "Order %s rejected due to invalid pickup zone %s",
            order_id,
            normalized_zone,
            extra={
                "order.id": order_id,
                "pickup.zone": normalized_zone,
                "taxi.tariff": request.tariff,
                "error.type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    dispatch_payload = {
        "order_id": order_id,
        "passenger_id": request.passenger_id,
        "pickup_zone": normalized_zone,
        "tariff": request.tariff,
        "estimated_price": estimated_price,
        "simulate_slow": request.simulate_slow,
        "simulate_error": request.simulate_error,
    }

    http_client: httpx.AsyncClient = app.state.http_client

    try:
        response = await http_client.post("/api/v1/dispatch", json=dispatch_payload)
    except httpx.RequestError as exc:
        orders_counter.add(
            1,
            attributes={
                "tariff": request.tariff,
                "pickup_zone": normalized_zone,
                "status": "error",
            },
        )
        logger.error(
            "Dispatch service unavailable while processing order %s",
            order_id,
            extra={
                "order.id": order_id,
                "pickup.zone": normalized_zone,
                "taxi.tariff": request.tariff,
                "dispatch.url": DISPATCH_SERVICE_URL,
                "error.type": type(exc).__name__,
                "error.message": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dispatch service is currently unavailable. Please retry later.",
        ) from exc

    if response.status_code >= 500:
        orders_counter.add(
            1,
            attributes={
                "tariff": request.tariff,
                "pickup_zone": normalized_zone,
                "status": "error",
            },
        )
        upstream_detail = response.text
        try:
            upstream_detail = response.json().get("detail", response.text)
        except ValueError:
            pass

        logger.error(
            "Dispatch service returned server error %s for order %s",
            response.status_code,
            order_id,
            extra={
                "order.id": order_id,
                "pickup.zone": normalized_zone,
                "taxi.tariff": request.tariff,
                "upstream.status_code": response.status_code,
                "upstream.detail": upstream_detail,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Driver dispatch failed: {upstream_detail}",
        )

    if response.status_code >= 400:
        orders_counter.add(
            1,
            attributes={
                "tariff": request.tariff,
                "pickup_zone": normalized_zone,
                "status": "error",
            },
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text,
        )

    dispatch_data = response.json()
    driver = AssignedDriver(**dispatch_data["driver"])

    orders_counter.add(
        1,
        attributes={
            "tariff": request.tariff,
            "pickup_zone": normalized_zone,
            "status": "success",
        },
    )

    logger.info(
        "Order %s confirmed and assigned to driver %s",
        order_id,
        driver.driver_id,
        extra={
            "order.id": order_id,
            "passenger.id": request.passenger_id,
            "driver.id": driver.driver_id,
            "pickup.zone": normalized_zone,
            "taxi.tariff": request.tariff,
            "order.estimated_price": estimated_price,
        },
    )

    return OrderResponse(
        order_id=order_id,
        passenger_id=request.passenger_id,
        pickup_zone=normalized_zone,
        tariff=request.tariff,
        estimated_price=estimated_price,
        status="driver_assigned",
        driver=driver,
    )