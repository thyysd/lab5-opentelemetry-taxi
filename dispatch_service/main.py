from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, HTTPException, status
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel

from common.telemetry import setup_telemetry

SERVICE_NAME = "dispatch-service"

FAULT_SLOW_PROBABILITY = float(os.getenv("FAULT_SLOW_PROBABILITY", "0.0"))
FAULT_ERROR_PROBABILITY = float(os.getenv("FAULT_ERROR_PROBABILITY", "0.0"))

telemetry_runtime = setup_telemetry(service_name=SERVICE_NAME)

logger = logging.getLogger(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

dispatch_duration_histogram = meter.create_histogram(
    name="driver_dispatch_duration_ms",
    unit="ms",
    description="Duration of searching and assigning a taxi driver in milliseconds",
)


class NoDriversAvailableError(RuntimeError):


@dataclass(frozen=True)
class DriverCandidate:
    driver_id: str
    driver_name: str
    car_model: str
    zone: str
    tariff: str
    rating: float
    distance_km: float


DRIVER_POOL: list[DriverCandidate] = [
    DriverCandidate("drv-101", "Алексей Смирнов", "Kia Rio", "center", "economy", 4.82, 1.4),
    DriverCandidate("drv-102", "Дмитрий Иванов", "Hyundai Solaris", "center", "economy", 4.91, 0.9),
    DriverCandidate("drv-103", "Сергей Кузнецов", "Skoda Octavia", "center", "comfort", 4.95, 1.1),
    DriverCandidate("drv-104", "Андрей Попов", "Toyota Camry", "center", "comfort", 4.88, 0.7),
    DriverCandidate("drv-105", "Михаил Соколов", "Mercedes E-Class", "center", "business", 4.98, 1.3),
    DriverCandidate("drv-106", "Иван Лебедев", "BMW 5 Series", "center", "business", 4.93, 0.8),
    DriverCandidate("drv-201", "Николай Морозов", "VW Polo", "north", "economy", 4.85, 1.6),
    DriverCandidate("drv-202", "Павел Волков", "Skoda Octavia", "north", "comfort", 4.90, 1.2),
    DriverCandidate("drv-203", "Олег Фёдоров", "Audi A6", "north", "business", 4.94, 2.1),
    DriverCandidate("drv-301", "Роман Орлов", "Renault Logan", "south", "economy", 4.79, 1.5),
    DriverCandidate("drv-302", "Егор Зайцев", "Kia K5", "south", "comfort", 4.92, 1.0),
    DriverCandidate("drv-303", "Виктор Павлов", "Mercedes E-Class", "south", "business", 4.97, 1.9),
    DriverCandidate("drv-401", "Артём Семёнов", "Hyundai Solaris", "airport", "economy", 4.87, 0.6),
    DriverCandidate("drv-402", "Максим Егоров", "Toyota Camry", "airport", "comfort", 4.96, 0.5),
    DriverCandidate("drv-403", "Денис Козлов", "BMW 5 Series", "airport", "business", 4.99, 0.4),
]


class DispatchRequest(BaseModel):
    order_id: str
    passenger_id: str
    pickup_zone: str
    tariff: Literal["economy", "comfort", "business"]
    estimated_price: float
    simulate_slow: bool = False
    simulate_error: bool = False


class AssignedDriverResponse(BaseModel):
    driver_id: str
    driver_name: str
    car_model: str
    rating: float
    distance_km: float
    eta_minutes: int


class DispatchResponse(BaseModel):
    order_id: str
    driver: AssignedDriverResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        telemetry_runtime.shutdown()


app = FastAPI(
    title="Taxi Dispatch Service",
    version="1.0.0",
    lifespan=lifespan,
)

FastAPIInstrumentor.instrument_app(app)


async def search_available_drivers(
    order_id: str,
    pickup_zone: str,
    tariff: str,
    simulate_slow: bool,
    simulate_error: bool,
) -> list[DriverCandidate]:
    should_slow = simulate_slow or (
        FAULT_SLOW_PROBABILITY > 0 and random.random() < FAULT_SLOW_PROBABILITY
    )
    should_error = simulate_error or (
        FAULT_ERROR_PROBABILITY > 0 and random.random() < FAULT_ERROR_PROBABILITY
    )

    with tracer.start_as_current_span(
        "search_available_drivers",
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("pickup.zone", pickup_zone)
        span.set_attribute("taxi.tariff", tariff)
        span.set_attribute("fault.simulate_slow", should_slow)
        span.set_attribute("fault.simulate_error", should_error)

        if should_slow:
            delay_seconds = round(random.uniform(1.5, 2.5), 2)
            span.add_event(
                "slow_driver_search_simulated",
                attributes={"delay.seconds": delay_seconds},
            )
            logger.warning(
                "Simulating slow geospatial search for order %s (%s s)",
                order_id,
                delay_seconds,
                extra={
                    "order.id": order_id,
                    "pickup.zone": pickup_zone,
                    "taxi.tariff": tariff,
                    "delay.seconds": delay_seconds,
                },
            )
            await asyncio.sleep(delay_seconds)
        else:
            await asyncio.sleep(random.uniform(0.03, 0.08))

        if should_error:
            exc = NoDriversAvailableError(
                f"No available drivers found in zone '{pickup_zone}' for tariff '{tariff}'"
            )
            span.set_attribute("drivers.candidates_count", 0)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise exc

        candidates = [
            drv
            for drv in DRIVER_POOL
            if drv.zone == pickup_zone and drv.tariff == tariff
        ]
        span.set_attribute("drivers.candidates_count", len(candidates))

        if not candidates:
            exc = NoDriversAvailableError(
                f"Driver pool is empty for zone '{pickup_zone}' and tariff '{tariff}'"
            )
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise exc

        logger.info(
            "Found %d candidate drivers for order %s",
            len(candidates),
            order_id,
            extra={
                "order.id": order_id,
                "pickup.zone": pickup_zone,
                "taxi.tariff": tariff,
                "drivers.candidates_count": len(candidates),
            },
        )
        return candidates


async def assign_best_driver(
    order_id: str,
    candidates: list[DriverCandidate],
) -> AssignedDriverResponse:
    with tracer.start_as_current_span(
        "assign_best_driver",
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        await asyncio.sleep(random.uniform(0.02, 0.05))

        best = max(
            candidates,
            key=lambda drv: (drv.rating * 2.0) - (drv.distance_km * 0.7),
        )
        eta_minutes = max(2, round(best.distance_km * 3.5))

        span.set_attribute("order.id", order_id)
        span.set_attribute("driver.id", best.driver_id)
        span.set_attribute("driver.rating", best.rating)
        span.set_attribute("driver.distance_km", best.distance_km)

        logger.info(
            "Assigned driver %s to order %s",
            best.driver_id,
            order_id,
            extra={
                "order.id": order_id,
                "driver.id": best.driver_id,
                "driver.rating": best.rating,
                "driver.distance_km": best.distance_km,
                "driver.eta_minutes": eta_minutes,
            },
        )

        return AssignedDriverResponse(
            driver_id=best.driver_id,
            driver_name=best.driver_name,
            car_model=best.car_model,
            rating=best.rating,
            distance_km=best.distance_km,
            eta_minutes=eta_minutes,
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/api/v1/dispatch", response_model=DispatchResponse)
async def dispatch_driver(request: DispatchRequest) -> DispatchResponse:
    start_time = time.perf_counter()
    dispatch_status = "success"

    logger.info(
        "Dispatch request received for order %s",
        request.order_id,
        extra={
            "order.id": request.order_id,
            "passenger.id": request.passenger_id,
            "pickup.zone": request.pickup_zone,
            "taxi.tariff": request.tariff,
            "order.estimated_price": request.estimated_price,
        },
    )

    try:
        candidates = await search_available_drivers(
            order_id=request.order_id,
            pickup_zone=request.pickup_zone,
            tariff=request.tariff,
            simulate_slow=request.simulate_slow,
            simulate_error=request.simulate_error,
        )
        assigned = await assign_best_driver(
            order_id=request.order_id,
            candidates=candidates,
        )
        return DispatchResponse(order_id=request.order_id, driver=assigned)

    except NoDriversAvailableError as exc:
        dispatch_status = "error"
        logger.error(
            "Failed to dispatch driver for order %s",
            request.order_id,
            extra={
                "order.id": request.order_id,
                "pickup.zone": request.pickup_zone,
                "taxi.tariff": request.tariff,
                "error.type": type(exc).__name__,
                "error.message": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    finally:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        dispatch_duration_histogram.record(
            elapsed_ms,
            attributes={
                "tariff": request.tariff,
                "pickup_zone": request.pickup_zone,
                "status": dispatch_status,
            },
        )