"""SimPy production-line model.

Building blocks
---------------
* ``BufferNode``  – FIFO queue or accumulating conveyor with finite capacity.
* ``Machine``     – one server of a station: setup, processing, preempt-resume
                    breakdowns, blocking-after-service, optional operator.
* ``Source``      – blocking source that releases products into the first buffer.

Breakdowns are delivered as SimPy interrupts. Every place a machine can wait goes
through ``Machine._await`` / ``Machine._work`` which handle an interrupt safely:
pending resource requests are cancelled (so no part is ever silently lost) and
interrupted work resumes with its remaining time after repair.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Generator
from typing import Any

import simpy

from .config import SINK_ID, Buffer, Scenario, Station
from .distributions import Sampler, Streams, make_sampler
from .monitor import Listener, MachineState, Monitor, StateTracker, Tally, TimeWeighted

SimGen = Generator[simpy.Event, Any, Any]

DEFAULT_INJECTED_REPAIR = 300.0


class Product:
    __slots__ = ("created", "entered_at", "id", "last_output", "ready_at", "reworks", "variant")

    def __init__(self, pid: int, variant: str, created: float) -> None:
        self.id = pid
        self.variant = variant
        self.created = created
        self.reworks = 0
        self.entered_at = created
        self.ready_at = created
        # Closed networks: when this entity last completed the output station.
        self.last_output = created


class BufferNode:
    def __init__(self, env: simpy.Environment, cfg: Buffer, monitor: Monitor, transit: Sampler | None = None) -> None:
        self.env = env
        self.cfg = cfg
        self.id = cfg.id
        self.monitor = monitor
        self._transit = transit
        self._store: simpy.Store = simpy.Store(env)
        # Slots are released only when a part physically leaves (after any conveyor
        # transit), so capacity is honoured while a downstream machine waits on it.
        self._space = simpy.Container(env, capacity=cfg.capacity, init=cfg.capacity) if cfg.capacity else None
        self.level = TimeWeighted(env.now)
        self.wait = Tally()
        # Time spent queueing after travel finished (e.g. trucks waiting at the excavator).
        self.queue_delay = Tally()
        self.entries = 0
        # Parts taken off the store by a machine but still travelling (conveyor).
        self.picked: list[Product] = []

    # Upstream side -------------------------------------------------------------------
    def reserve(self) -> simpy.Event:
        if self._space is None:
            return self.env.timeout(0)
        return self._space.get(1)

    def deposit(self, product: Product) -> None:
        now = self.env.now
        product.entered_at = now
        product.ready_at = now + (self._transit() if self._transit is not None else self.cfg.transit_time)
        self._store.put(product)
        self.level.add(now, 1)
        self.entries += 1
        if self.monitor.streaming:
            self.monitor.emit(
                {"type": "enter", "t": now, "product": product.id, "buffer": self.id, "ready_at": product.ready_at}
            )

    # Downstream side -----------------------------------------------------------------
    def request(self) -> simpy.Event:
        return self._store.get()

    def release(self, product: Product) -> None:
        now = self.env.now
        if self._space is not None:
            self._space.put(1)
        self.level.add(now, -1)
        self.wait.add(now - product.entered_at)
        self.queue_delay.add(max(0.0, now - product.ready_at))
        self.picked.remove(product)

    def contents(self) -> list[Product]:
        """Parts occupying the buffer, front (oldest) first."""
        return [*self.picked, *self._store.items]

    @property
    def count(self) -> int:
        return int(self.level.value)

    def reset_stats(self, t: float) -> None:
        self.level.reset(t)
        self.wait = Tally()
        self.queue_delay = Tally()
        self.entries = 0


class Machine:
    def __init__(self, model: FactoryModel, station: StationNode, index: int) -> None:
        self.model = model
        self.env = model.env
        self.station = station
        self.cfg = station.cfg
        self.id = f"{self.cfg.id}#{index + 1}"
        self.tracker = StateTracker(self.env.now, MachineState.STARVED)
        self.product: Product | None = None
        self.last_variant: str | None = None
        self.processed = 0
        self.failures = 0
        self.processing_time = 0.0  # actual time in PROCESSING, used for OEE

        streams = model.streams
        self._process: dict[str, Sampler] = {
            v.id: make_sampler(
                self.cfg.process_time_by_variant.get(v.id, self.cfg.process_time),
                streams.rng(f"{self.id}:process:{v.id}"),
            )
            for v in model.scenario.variants
        }
        self._setup = (
            make_sampler(self.cfg.setup_time, streams.rng(f"{self.id}:setup")) if self.cfg.setup_time else None
        )
        failure = self.cfg.failure
        self._ttf = make_sampler(failure.ttf, streams.rng(f"{self.id}:ttf")) if failure else None
        self._ttr = make_sampler(failure.ttr, streams.rng(f"{self.id}:ttr")) if failure else None

        self._down_event: simpy.Event | None = None
        self._forced_repair: float | None = None
        self._operator_req: simpy.resources.resource.Request | None = None

        self.proc = self.env.process(self._run())
        if self._ttf is not None:
            self.env.process(self._breaker())

    # State ---------------------------------------------------------------------------
    @property
    def state(self) -> MachineState:
        return self.tracker.state

    def _set_state(self, state: MachineState) -> None:
        if state == self.tracker.state:
            return
        self.tracker.change(self.env.now, state)
        if self.model.monitor.streaming:
            self.model.monitor.emit({"type": "state", "t": self.env.now, "machine": self.id, "state": state.value})

    # Failures ------------------------------------------------------------------------
    def fail(self, repair_time: float | None = None) -> bool:
        """Break the machine now. Returns False if it is already down."""
        if self._down_event is not None:
            return False
        self._down_event = self.env.event()
        self._forced_repair = repair_time
        self.proc.interrupt("failure")
        return True

    def _breaker(self) -> SimGen:
        assert self._ttf is not None
        while True:
            yield self.env.timeout(self._ttf())
            if self._down_event is None:
                self.fail()
            assert self._down_event is not None
            yield self._down_event

    def _breakdown(self) -> SimGen:
        resume_state = self.state
        self._set_state(MachineState.DOWN)
        self.failures += 1
        # An operator is not tied up while a technician repairs the machine.
        held_operator = self._operator_req is not None
        if held_operator:
            self._release_operator()

        if self._forced_repair is not None:
            repair = self._forced_repair
        elif self._ttr is not None:
            repair = self._ttr()
        else:
            repair = DEFAULT_INJECTED_REPAIR
        techs = self.model.technicians
        if techs is not None:
            with techs.request() as req:
                yield req
                yield self.env.timeout(repair)
        else:
            yield self.env.timeout(repair)

        done, self._down_event, self._forced_repair = self._down_event, None, None
        assert done is not None
        done.succeed()
        if held_operator:
            self._set_state(MachineState.WAITING_OPERATOR)
            yield from self._acquire_operator()
        self._set_state(resume_state)

    # Interrupt-safe waiting ----------------------------------------------------------
    def _await(self, make_event: Callable[[], simpy.Event]) -> SimGen:
        """Wait for an event created by ``make_event``; returns the granted event.

        The event itself (not its value) is returned because a resource
        ``Request`` must be kept to release it later.
        """
        event = make_event()
        while True:
            try:
                yield event
                return event
            except simpy.Interrupt:
                if event.triggered:
                    # Request was granted in the same instant as the failure: keep it.
                    yield from self._breakdown()
                    return event
                cancel = getattr(event, "cancel", None)
                if cancel is not None:
                    cancel()
                yield from self._breakdown()
                event = make_event()

    def _work(self, duration: float) -> SimGen:
        """Spend ``duration`` of productive time; failures pause and resume it."""
        remaining = duration
        while remaining > 1e-9:
            started = self.env.now
            try:
                yield self.env.timeout(remaining)
                remaining = 0.0
            except simpy.Interrupt:
                remaining -= self.env.now - started
                yield from self._breakdown()

    def _acquire_operator(self) -> SimGen:
        operators = self.model.operators
        assert operators is not None
        self._operator_req = yield from self._await(operators.request)

    def _release_operator(self) -> None:
        if self._operator_req is not None and self.model.operators is not None:
            self.model.operators.release(self._operator_req)
        self._operator_req = None

    # Main loop -----------------------------------------------------------------------
    def _run(self) -> SimGen:
        env, monitor = self.env, self.model.monitor
        inp = self.station.input
        while True:
            self._set_state(MachineState.STARVED)
            product: Product = (yield from self._await(inp.request)).value
            inp.picked.append(product)
            # On a conveyor the part must finish travelling before it can be picked.
            yield from self._work(product.ready_at - env.now)
            inp.release(product)
            self.product = product
            if monitor.streaming:
                monitor.emit({"type": "take", "t": env.now, "product": product.id, "machine": self.id})

            if self.cfg.needs_operator:
                self._set_state(MachineState.WAITING_OPERATOR)
                yield from self._acquire_operator()

            if self._setup is not None and self.last_variant not in (None, product.variant):
                self._set_state(MachineState.SETUP)
                yield from self._work(self._setup())
            self.last_variant = product.variant

            self._set_state(MachineState.PROCESSING)
            duration = self._process[product.variant]()
            yield from self._work(duration)
            self.processing_time += duration
            self.processed += 1
            if self.cfg.needs_operator:
                self._release_operator()

            target = self.station.route(product)
            if self.station.is_output:
                self.model.record_output(product)
            if target is None or target == SINK_ID:
                self.product = None
                self.model.retire(product, scrapped=target is None, machine=self.id)
                continue
            self._set_state(MachineState.BLOCKED)
            buffer = self.model.buffers[target]
            yield from self._await(buffer.reserve)
            self.product = None
            buffer.deposit(product)

    def reset_stats(self, t: float) -> None:
        self.tracker.reset(t)
        self.processed = 0
        self.failures = 0
        self.processing_time = 0.0


class StationNode:
    def __init__(self, model: FactoryModel, cfg: Station) -> None:
        self.cfg = cfg
        self.id = cfg.id
        self.model = model
        self.input = model.buffers[cfg.input]
        self.is_output = model.scenario.output_station == cfg.id
        self.passed = 0
        self.reworked = 0
        self.scrapped = 0
        self._qc_rng = model.streams.rng(f"{cfg.id}:qc") if cfg.qc else None
        self.machines = [Machine(model, self, i) for i in range(cfg.servers)]

    def route(self, product: Product) -> str | None:
        """Destination id for a finished part, or None if it is scrapped."""
        qc = self.cfg.qc
        if qc is None or self._qc_rng is None:
            self.passed += 1
            return self.cfg.output
        u = float(self._qc_rng.random())
        if u < qc.pass_prob:
            self.passed += 1
            return self.cfg.output
        if u < qc.pass_prob + qc.rework_prob:
            self.reworked += 1
            product.reworks += 1
            self.model.monitor.reworked += 1
            return qc.rework_output
        self.scrapped += 1
        return None

    def reset_stats(self, t: float) -> None:
        self.passed = self.reworked = self.scrapped = 0
        for m in self.machines:
            m.reset_stats(t)


class Source:
    def __init__(self, model: FactoryModel) -> None:
        self.model = model
        cfg = model.scenario.source
        self.cfg = cfg
        self.output = model.buffers[cfg.output]
        self._interarrival = make_sampler(cfg.interarrival, model.streams.rng("source:interarrival"))
        variants = model.scenario.variants
        self._variant_ids = [v.id for v in variants]
        total = sum(v.weight for v in variants)
        self._variant_p = [v.weight / total for v in variants]
        self._variant_rng = model.streams.rng("source:variant")
        self.blocked_time = 0.0
        model.env.process(self._run())

    def _run(self) -> SimGen:
        env, model = self.model.env, self.model
        limit = self.cfg.max_arrivals
        for _ in itertools.count() if limit is None else range(limit):
            yield env.timeout(self._interarrival())
            if len(self._variant_ids) == 1:
                variant = self._variant_ids[0]
            else:
                variant = self._variant_ids[int(self._variant_rng.choice(len(self._variant_ids), p=self._variant_p))]
            # Blocking source: if the first buffer is full the release waits.
            asked = env.now
            yield self.output.reserve()
            self.blocked_time += env.now - asked
            product = model.new_product(variant)
            if model.monitor.streaming:
                model.monitor.emit({"type": "create", "t": env.now, "product": product.id, "variant": variant})
            self.output.deposit(product)

    def reset_stats(self, t: float) -> None:
        self.blocked_time = 0.0


class FactoryModel:
    def __init__(
        self,
        scenario: Scenario,
        seed: int | None = None,
        listener: Listener | None = None,
        env: simpy.Environment | None = None,
    ) -> None:
        self.scenario = scenario
        self.seed = scenario.run.seed if seed is None else seed
        self.env = env or simpy.Environment()
        self.streams = Streams(self.seed)
        self.monitor = Monitor(listener)
        self._next_id = itertools.count(1)
        self.products: dict[int, Product] = {}  # parts currently in the system

        res = scenario.resources
        self.technicians = simpy.Resource(self.env, res.technicians) if res.technicians else None
        self.operators = simpy.Resource(self.env, res.operators) if res.operators else None

        self.buffers = {
            b.id: BufferNode(
                self.env,
                b,
                self.monitor,
                make_sampler(b.transit, self.streams.rng(f"{b.id}:transit")) if b.transit is not None else None,
            )
            for b in scenario.buffers
        }
        self.stations = {s.id: StationNode(self, s) for s in scenario.stations}
        self.machines = {m.id: m for st in self.stations.values() for m in st.machines}
        self.source = Source(self)
        self.warmed_up = False

    # Lifecycle hooks used by nodes ---------------------------------------------------
    def new_product(self, variant: str) -> Product:
        now = self.env.now
        product = Product(next(self._next_id), variant, now)
        self.products[product.id] = product
        mon = self.monitor
        mon.created += 1
        mon.total_created += 1
        mon.wip.add(now, 1)
        return product

    def _count_output(self, product: Product, cycle_time: float) -> None:
        mon = self.monitor
        mon.completed += 1
        mon.total_completed += 1
        mon.completion_times.append(self.env.now)
        mon.completion_cycle_times.append(cycle_time)
        mon.cycle_time.add(cycle_time)
        mon.cycle_time_by_variant.setdefault(product.variant, Tally()).add(cycle_time)

    def record_output(self, product: Product) -> None:
        """Closed networks: an entity completed the output station (e.g. a truck dumped).

        Cycle time is the time since this entity's previous output, i.e. one full
        loop such as a truck's load-haul-dump-return cycle.
        """
        now = self.env.now
        self._count_output(product, now - product.last_output)
        product.last_output = now

    def retire(self, product: Product, *, scrapped: bool, machine: str) -> None:
        now = self.env.now
        mon = self.monitor
        self.products.pop(product.id, None)
        mon.wip.add(now, -1)
        mon.total_exited += 1
        if scrapped:
            mon.scrapped += 1
            mon.total_scrapped += 1
        elif not self.scenario.is_closed:
            self._count_output(product, now - product.created)
        if mon.streaming:
            mon.emit(
                {
                    "type": "exit",
                    "t": now,
                    "product": product.id,
                    "machine": machine,
                    "outcome": "scrap" if scrapped else "complete",
                }
            )

    # Running -------------------------------------------------------------------------
    def reset_stats(self) -> None:
        t = self.env.now
        self.monitor.reset(t)
        for b in self.buffers.values():
            b.reset_stats(t)
        for s in self.stations.values():
            s.reset_stats(t)
        self.source.reset_stats(t)
        self.warmed_up = True

    def run(self, horizon: float | None = None, warmup: float | None = None) -> None:
        """Run a complete batch replication: warm-up, reset, then measure to horizon."""
        horizon = self.scenario.run.horizon if horizon is None else horizon
        warmup = self.scenario.run.warmup if warmup is None else warmup
        if warmup >= horizon:
            raise ValueError("warmup must be shorter than horizon")
        if warmup > 0:
            self.env.run(until=warmup)
            self.reset_stats()
        self.env.run(until=horizon)

    def advance(self, until: float) -> None:
        """Advance a live simulation, applying the warm-up reset when crossed."""
        warmup = self.scenario.run.warmup
        if not self.warmed_up and warmup > 0 and self.env.now < warmup <= until:
            self.env.run(until=warmup)
            self.reset_stats()
        if until > self.env.now:
            self.env.run(until=until)

    def settle(self) -> None:
        """Process events due at the current instant without advancing time."""
        env = self.env
        while env.peek() == env.now:
            env.step()

    def inject_failure(self, machine_id: str, repair_time: float | None = None) -> bool:
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        return machine.fail(repair_time)

    def snapshot(self) -> dict[str, Any]:
        """Full current state, sent to a client on (re)connect."""
        return {
            "t": self.env.now,
            "machines": {
                m.id: {"state": m.state.value, "product": m.product.id if m.product else None}
                for m in self.machines.values()
            },
            "buffers": {
                b.id: [{"product": p.id, "entered_at": p.entered_at, "ready_at": p.ready_at} for p in b.contents()]
                for b in self.buffers.values()
            },
            "products": {p.id: p.variant for p in self.products.values()},
        }
