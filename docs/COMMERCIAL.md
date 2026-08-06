# Commercial architecture

What this could be sold as, and how the system would have to change to
support that. Written to be argued with — the recommendation at the bottom of
each section is a position, not a conclusion.

The short version: **the robot is not the product.** Robotic panel cleaning is
a served market with incumbents who have field hours, module-manufacturer
approvals and installed fleets. Competing as "a better cleaning robot" means
competing on manufacturing and service against companies that already do both.
The software is further ahead than the hardware, so sell from that side.

---

## Assets and liabilities

Be honest about both columns, because the commercial shape follows from which
one is longer.

| Asset | Why it is worth something |
|---|---|
| The HAL (`hal/base.py`) | Cycle logic runs identically against a Pi or a simulator. A control change can be proven without a machine, a roof, or a sunny afternoon. |
| The simulator (`hal/sim.py`) | Ten injectable faults, a full cycle in ~20 ms. Competitors who built the robot first mostly lack this, and retrofitting it means inserting a hardware seam under years of code that assumes real pins. |
| The safety chain (`safety.py`) | Interlocks raise rather than returning a status a caller can ignore. This is the difference between a demo and something you can point at somebody's roof. |
| Per-run telemetry (`telemetry.py`) | Every cycle writes a structured trace. Raw material for the only report a customer actually pays for. |

| Liability | What it costs |
|---|---|
| Uncommissioned hardware | Zero field hours means zero credibility with an asset owner, and no soiling data of your own. See [COMMISSIONING.md](COMMISSIONING.md). |
| No encoder on the built machine | Position is dead-reckoned, so stall protection is unavailable and the software refuses unattended runs. Unattended operation is the entire value proposition. A parts-order problem, but on the critical path. |
| Single-axis, tethered, rail-guided | It cleans the panel it is mounted on. Every rail is capex per row. |
| Water | Pulsed water needs a tethered supply. Utility-scale sites are frequently where water is expensive or absent, which is why waterless designs won there. |

---

## Four shapes, not four stages

These are different companies with different capital needs and different first
hires. Choosing late is what kills hardware projects.

| Shape | What you sell | What it demands | Fit |
|---|---|---|---|
| Sell robots | Hardware per unit, plus spares and service | Manufacturing, certification, inventory, RMA, a service van. Margin lives in volume you do not have. | Worst |
| Cleaning as a service | Clean panels, billed per MW per cycle | You own the machines and the labour: trucks, insurance, technicians, working capital. A real business, but an operations business. | Viable, capital-hungry |
| **Control & fleet layer** | **The software that runs somebody else's cleaning hardware safely and provably** | **A clean device API, per-device identity, multi-tenancy. All software.** | **Best** |
| Soiling economics | The report that proves cleaning paid for itself | Inverter integrations and a defensible measurement method. Thin alone. | The wedge |

Recommendation: the third, with the fourth as the way in. Nobody wakes up
wanting fleet-management software. They wake up wanting to know whether
cleaning is worth what it costs — and answering that needs exactly the
telemetry these machines already emit.

---

## Three tiers, ordered by distance from the motor

Each tier is one step further from the hardware, and each step further out is
allowed to do strictly less.

```
  TIER 3  cloud       registry · telemetry store · console · releases · billing
             │        multi-tenant, deliberately the weakest in authority
             │  mTLS · MQTT · survives days offline
  TIER 2  site        scheduler · weather + tariff · store & forward
             │        one gateway per array; owns decisions that must hold
             │        when the WAN drops
  ═══════════╪══════════════ TRUST BOUNDARY ═══════════════════════════════
             │  intents only — nothing above this line energises an output
  TIER 1  edge        safety chain · cycle logic · Board interface
                      local buffer · OTA agent
                      one per machine, authoritative, correct with the
                      network unplugged
```

Delete the top two tiers and the machine still cleans the panel correctly.
That property is the design, not a side effect.

**What tier 1 gains for a fleet:** a store-and-forward telemetry queue that
survives days offline, a per-device identity, and an update agent that can roll
itself back. Nothing in the interlock chain moves.

---

## Commands are intents, and the machine may refuse

Today `/api/start` begins a cycle. Correct for one machine on loopback, wrong
across a WAN for two reasons that both bite in the field:

1. A command sent to an offline machine is not cancelled — it is queued.
   Without an expiry it fires when the link returns, which may be six hours
   later, in the dark, in the rain.
2. A cloud that can actuate is a cloud that can be breached into actuating.

So the cloud enqueues an intent — an operation, a validity window, a nonce and
a signature — and the edge decides whether to honour it against interlocks the
cloud cannot see.

```
operator ──▶ signed intent ──queued──▶ edge validates ──┬──▶ cycle runs
             op: full_cycle            signature, clock,│    telemetry streams back
             valid_until: +20 min      then every       │
             nonce · device sig        interlock        └──▶ refused, with a reason
                                                             rain · expired · e-stop
                                                             no encoder · min gap
```

A refusal is a result, not an error, and it returns to the operator either way.

**What this costs in the current code:** less than it sounds. `CycleRefused`
already exists and the refusal reasons are already enumerated — minimum gap,
rain, no stall protection. The work is wrapping the existing POST handlers in a
verify-then-delegate step and giving refusals a stable machine-readable code so
the cloud can aggregate them.

---

## Identity does not survive a fleet

`SOLARSWEEP_WEB_TOKEN` is a single shared secret. Correct for one machine on a
home network; it fails in a predictable order.

| Fleet size | What breaks | What replaces it |
|---|---|---|
| 1 | Nothing. Loopback means the only client is the machine itself. | Shared token, as built. |
| 2–20 | One token across all machines. A technician who leaves takes the keys to every site; rotating means visiting each device. | Per-device credential, centrally revocable. |
| 20–500 | You cannot prove which machine sent a telemetry record, so you cannot bill on it or trust it in a dispute. | Per-device X.509 provisioned at assembly; mTLS to the gateway. The certificate is the device's name. |
| 500+ | Customers ask who at your company can move their machines. The honest answer is "anyone with the token". | Signed intents with per-operator keys, and an audit log the customer can read. |

Do this at twenty machines, not five hundred. Retrofitting identity onto
deployed hardware means physically visiting every device.

---

## Fleet releases are the defensible part

The simulator stops being a testing convenience here and becomes the reason
somebody buys.

```
commit ──▶ simulated fleet ──▶ canary ──▶ stage ──▶ fleet
142 tests   1000 machines       1% · 24h   10% · 72h   100%
no hardware 10 faults injected      │          │
            minutes, not weeks      └──────────┴──▶ auto-rollback if the fault
                                                    rate rises against the
                                                    previous build
```

The gate between stages is a measured fault rate, not a human deciding it looks
fine. Every stage can fall back to the build it replaced.

---

## The number that closes the sale

```
annual_kwh     = MWp × 1000 × specific_yield
recovered_kwh  = annual_kwh × soiling_recovered_pct / 100
revenue        = recovered_kwh × energy_price
spend          = MWp × cost_per_MW_cycle × cycles_per_year
net            = revenue − spend
```

The arithmetic is trivial. What is hard — and what nobody can hand you from a
spreadsheet — is `soiling_recovered_pct`, because it depends on the site's
dust, rainfall, tilt and cleaning frequency. It has to be measured, which is
why ninety days of your own telemetry matters more than another feature.

Run the numbers honestly and note what falls out: **at low tariffs and low
soiling, cleaning does not pay at any frequency.** The right answer to that
customer is that they should not buy. Being the vendor who says so is worth
more than the one sale.

---

## What would kill it

- **Panel warranties.** Anything that touches the glass can void one. Module
  manufacturers approve cleaning methods, and approval is slow, expensive and a
  genuine moat for whoever already has it. Find out what approval requires
  before building anything else.
- **Water is the wrong bet at utility scale.** Large deployments went waterless
  because desert sites have sun and no water. Pulsed water suits rooftop and
  commercial installations with a tap — a real market, but say which market you
  are in and design for it.
- **Rail per row is capex.** A machine that cannot leave its own row means the
  customer buys rails for every row. Model that against a crew with brushes
  before assuming the robot wins.
- **Insurance and liability.** A machine that moves unattended on somebody's
  roof is an insurance conversation before it is an engineering one. The safety
  chain and the audit log are the assets — another argument for the software
  framing.
- **The incumbents are not asleep.** Assume anything buildable in a year, they
  have. Compete where they are structurally weak: they sell closed hardware, so
  an open control layer that runs somebody else's machine is a thing they cannot
  offer without cannibalising themselves.

---

## Build order

Each step is cheap only because the one before it happened.

1. **Fit the encoder and commission the machine.** Everything downstream is a
   claim you cannot make until one cycle has run on real glass. It also enables
   stall protection, which is what makes unattended operation offerable.
2. **Run it ninety days and keep the telemetry.** Your own soiling curve from a
   real site is the thing you will sell against.
3. **Give refusals stable codes and put an expiry on commands.** Small change,
   and the precondition for anything remote. Before there is a second machine.
4. **Per-device identity.** At two machines this feels like overengineering. At
   twenty it is a weekend. At two hundred it is a truck roll to every site.
5. **The site gateway.** Only once enough machines share a location that the
   scheduler must think about them together.
6. **The soiling report as a standalone product.** Sell it to somebody who
   cleans manually. It works without any of your hardware, proves the
   measurement method, and gets you inside accounts that would not yet buy a
   robot.
7. **Fleet console and staged releases.** Last, because it is only worth
   building once there is a fleet — and by then you will know which twenty
   things actually needed watching.
