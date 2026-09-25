# Snake, Kombat, and Laya vs Jev API — IPO Explanation

> This document explains the project using **IPO**: **Input → Processing → Output**.
>
> The project calls the local model **Laya**. If “lava” was intended, this document assumes it means Laya.

## 1. Overall architecture

The Snake and Kombat games use the same decision pipeline:

```text
Game state
    ↓
State is converted into structured input
    ↓
Questions and state are sent to Laya or Jev
    ↓
The model returns typed answers
    ↓
Answers are converted into game commands
    ↓
The game simulation updates
    ↓
Canvas, HUD, scores, and JSON results are produced
```

Important files:

| File | Responsibility |
|---|---|
| `snake/game.js` | Snake physics, sensing, apple collisions, and rendering |
| `snake/index.html` | Snake page, agents, race setup, game loop, HUD, and export |
| `fight/fight.js` | Fighter state, attack timing, hit resolution, and rendering |
| `fight/index.html` | Fight page, questions, agents, fight setup, HUD, and export |
| `shared/agents.js` | Shared Laya/Jev request loop and human Snake controller |
| `server.py` | Static server, local Laya backend, and Jev API proxy |

---

# 2. Snake game using IPO

## 2.1 Snake input

### Game configuration

`snake/game.js` defines the core game rules in `CFG`:

```js
export const CFG = {
  speed: 150,
  sprint: 240,
  turn: 3.2,
  spacing: 4.6,
  startLen: 95,
  growPx: 36,
  apples: 3,
  passThroughSelf: true,
  wrapWalls: true
};
```

These values control:

- normal speed
- sprint speed
- turning speed
- body spacing
- initial length
- growth after eating an apple
- number of apples
- whether the snake can cross its own body
- whether the snake wraps around the edges

Because `passThroughSelf` and `wrapWalls` are `true`, this is not classic deadly Snake. The snake normally cannot die: it passes through itself and re-enters through the opposite edge.

### Seed input

The seed determines the apple layout:

```js
const seed = parseInt($('seed').value, 10) || 2026;
```

Both Snake arenas are reset with the same seed:

```js
for (const a of arenas) {
  a.resize();
  a.reset(seed);
}
```

This makes the apple sequence and initial setup comparable for both sides.

### Human keyboard input

A human can control the snake with:

- `A` or `ArrowLeft`: steer left
- `D` or `ArrowRight`: steer right
- `W`, `ArrowUp`, or `Shift`: sprint

This is processed by `HumanAgent.control()` in `shared/agents.js`:

```js
if (this.keys.has('arrowleft') || this.keys.has('a')) s -= 1;
if (this.keys.has('arrowright') || this.keys.has('d')) s += 1;

return {
  steer: s,
  sprint: this.keys.has('arrowup') ||
          this.keys.has('w') ||
          this.keys.has('shift')
};
```

### Model state input

For Laya or Jev, the game does not send a screenshot. It creates a structured state using `Arena.sense()`.

For the wrapping version, the state contains information such as:

```js
{
  arena: {
    width: this.W,
    height: this.H,
    edges: 'wrap around: leaving one edge re-enters from the opposite edge'
  },
  snake: {
    length_px: ...,
    speed_px_per_sec: ...
  },
  nearest_apple: {
    distance_px: ...,
    direction: ...,
    bearing_degrees: ...,
    is_behind_the_snake: ...,
    side: ...,
    shortest_route_goes_through_an_edge: ...
  }
}
```

The model therefore receives a compact description of the world instead of raw pixels.

### Snake questions

The questions are defined in `shared/agents.js`.

The first question asks for a steering choice:

```js
steer: {
  type: 'choice'
}
```

Possible answers are:

```text
HARD_LEFT
LEFT
STRAIGHT
RIGHT
HARD_RIGHT
```

The second question asks whether sprinting is appropriate:

```js
sprint: {
  type: 'noul'
}
```

A `noul` is used as a yes/no-style probability. The model returns the probability that sprinting is the correct choice.

---

## 2.2 Snake processing

### Step 1: Start and initialize the race

`startRace()` in `snake/index.html` builds the selected agents and resets both arenas:

```js
buildAgents();

for (const a of arenas) {
  a.resize();
  a.reset(seed);
}
```

The available agents are:

- Human keyboard control
- Laya through `/api/laya`
- Jev through `/api/jev`

### Step 2: Prime the models

Before the clock starts, each model receives one warm-up request:

```js
await Promise.all(agents.map(a => a.prime()));
```

This is done so that:

- the model has a decision before movement begins
- Laya checkpoint loading is not charged to the race
- the snake does not start with an empty command

### Step 3: Pace both sides using the slowest model

After priming, the game measures the slowest model latency:

```js
const lat = Math.max(
  0.35,
  ...agents.map(a => (a.p50 || a.lastLatency || 0) / 1000)
);
```

The arena is then paced using that latency:

```js
const v = arenas[0].setPacing(lat);
arenas[1].baseSpeed = v;
arenas[1].sprintSpeed = arenas[0].sprintSpeed;
arenas[1].turnRate = arenas[0].turnRate;
```

The slower model has fewer decisions per second, but the world is slowed so it is not also penalized by excessively fast movement.

### Step 4: Send state and questions to a model

`ModelAgent.pump()` repeatedly sends a request:

```js
const res = await fetch(this.endpoint, {
  method: 'POST',
  headers: apiHeaders(),
  body: JSON.stringify({
    model: this.model,
    state: this.senseState(),
    questions: this.questions
  })
});
```

A request has this general shape:

```json
{
  "model": "jev-latest",
  "state": {
    "arena": {
      "width": 800,
      "height": 500,
      "edges": "wrap around..."
    },
    "snake": {
      "length_px": 95,
      "speed_px_per_sec": 120
    },
    "nearest_apple": {
      "distance_px": 210,
      "bearing_degrees": -35,
      "side": "left"
    }
  },
  "questions": {
    "steer": { "...": "..." },
    "sprint": { "...": "..." }
  }
}
```

The requests are pipelined. The snake keeps moving on its last decision while new model requests are in flight.

Laya uses two workers in the page setup, while Jev uses three because Jev normally has a longer network round trip:

```js
// Laya
{ workers: 2 }

// Jev
{ workers: 3 }
```

### Step 5: Convert model answers into movement controls

When a response arrives, `ModelAgent.apply()` reads `body.answers`:

```js
const a = body.answers || {};
```

Steering labels are mapped to numbers:

```js
const STEER_VALUES = {
  HARD_LEFT: -1.0,
  LEFT: -0.35,
  STRAIGHT: 0.0,
  RIGHT: 0.35,
  HARD_RIGHT: 1.0
};
```

The selected steering value is applied gradually:

```js
const want = STEER_VALUES[a.steer.choice] ?? 0;
this.steer = this.steer * 0.35 + want * 0.65;
```

The sprint probability becomes a boolean command:

```js
const p = a.sprint.noul ?? 0;
this.sprintP = p;
this.sprint = p > 0.65;
```

Therefore:

```text
sprint probability > 0.65 → sprint
sprint probability <= 0.65 → normal movement
```

### Step 6: Apply a turn budget

A model response may be delayed. If the snake kept turning for the entire delay, it could rotate too far or circle an apple.

`ModelAgent.control()` limits the amount of turning made from one decision:

```js
const spent = Math.abs(
  this.arena.snake.ang - this.angAtDecision
);

if (spent >= budget) steer = 0;
```

The old command is held temporarily, then steering stops until the next answer arrives.

### Step 7: Update Snake physics

The animation loop calls:

```js
for (const a of arenas) a.step(dt);
```

`Arena.step()` obtains the current controller output:

```js
const c = this.controller(this) || {};
this.update(dt, c.steer || 0, !!c.sprint);
```

The actual movement occurs in `Arena.update()`.

#### Turning

```js
s.ang += steer * this.turnRate * dt;
```

#### Speed interpolation

```js
const target = sprint ? this.sprintSpeed : this.baseSpeed;
s.speed += (target - s.speed) * Math.min(1, dt * 6);
```

#### Head movement

```js
const nx = head.x + Math.cos(ang) * s.speed * dt;
const ny = head.y + Math.sin(ang) * s.speed * dt;
```

#### Body movement and growth

The body is represented by a list of spine points:

```js
s.spine.unshift({x: nx, y: ny});
```

If the snake is not growing, the tail point is removed:

```js
if (s.targetGrow > 0) {
  s.targetGrow--;
} else {
  s.spine.pop();
}
```

#### Apple collision

```js
if (
  Math.hypot(
    wrapDelta(h.x - a.x, this.W),
    wrapDelta(h.y - a.y, this.H)
  ) < CFG.headR + a.r
) {
  this.score += 10;
  s.targetGrow += CFG.growPer;
  this.apples.push(this.spawnApple());
}
```

When an apple is eaten:

1. the score increases by 10
2. the snake grows
3. a new apple is created
4. an `eat` event is emitted

---

## 2.3 Snake output

### Canvas

`Arena.draw()` displays:

- background
- apples
- snake body
- snake head
- particles
- wrapped copies of the snake

### HUD

`paintFrame()` displays:

- score
- length
- survival time
- race clock

`paintAgent()` displays:

- decision count
- decisions per second
- P50 latency
- steering probabilities
- sprint probability
- errors

### Winner

At the end of the fixed round:

```js
if (a1.score !== a2.score) {
  $('win' + (a1.score > a2.score ? 1 : 2))
    .textContent = 'WINNER';
} else {
  $('win1').textContent = $('win2').textContent = 'DRAW';
}
```

The winner is the side with the most apples.

### JSON export

The exported Snake result contains values such as:

```js
{
  seed,
  pace_px_per_sec,
  duration_sec,
  round_sec,
  sides: [
    {
      model,
      score,
      apples,
      length_nodes,
      decisions,
      decisions_per_sec,
      latency_p50_ms,
      latency_p95_ms,
      input_tokens,
      output_tokens
    }
  ]
}
```

---

# 3. Kombat game using IPO

## 3.1 Fight input

### Fighter configuration

`fight/fight.js` defines the fight rules in `FCFG`:

```js
export const FCFG = {
  walkSpeed: 210,
  jumpV: 620,
  gravity: 1700,
  health: 100,
  roundMs: 90000,
  moves: {
    PUNCH: {
      startup: 130,
      active: 90,
      recovery: 210,
      reach: 118,
      dmg: 7,
      push: 46
    },
    KICK: {
      startup: 240,
      active: 120,
      recovery: 360,
      reach: 168,
      dmg: 14,
      push: 96
    }
  },
  blockScale: 0.18
};
```

Each attack has three stages:

```text
startup → active → recovery
```

Punch is faster and weaker. Kick is slower, reaches farther, and deals more damage.

### Fight action input

The model chooses one action:

```text
ADVANCE
RETREAT
PUNCH
KICK
BLOCK
JUMP
```

These actions are defined in `fight/fight.js`:

```js
const ACTIONS = [
  'ADVANCE',
  'RETREAT',
  'PUNCH',
  'KICK',
  'BLOCK',
  'JUMP'
];
```

### Fight state input

Each fighter receives its own perspective through:

```js
ring.senseFor(this.who)
```

The state contains information about the fighter:

```js
me: {
  health,
  state,
  can_act_now,
  distance_to_the_wall_behind_me,
  cornered
}
```

It contains information about the opponent:

```js
opponent: {
  health,
  state,
  is_winding_up_an_attack,
  is_attacking_now,
  is_recovering_and_open,
  is_blocking,
  is_in_the_air
}
```

It also contains distance and range information:

```js
spacing: {
  gap_px,
  my_punch_reaches_at_px,
  my_kick_reaches_at_px,
  punch_would_connect,
  kick_would_connect,
  too_far_to_hit
}
```

The remaining time is included as:

```js
clock: {
  seconds_left
}
```

### Fight questions

The fight asks two questions, defined in `fight/index.html`.

The main question selects the action:

```js
action: {
  type: 'choice'
}
```

The second question asks whether this is a good moment to commit to a heavy attack:

```js
commit: {
  type: 'noul'
}
```

### Human keyboard input

Left fighter:

```text
A / D → move
F     → punch
G     → kick
H     → block
W     → jump
```

Right fighter:

```text
ArrowLeft / ArrowRight → move
K                     → punch
L                     → kick
;                     → block
ArrowUp               → jump
```

---

## 3.2 Fight processing

### Step 1: Create a fight-specific agent

`FightAgent` extends `ModelAgent`:

```js
class FightAgent extends ModelAgent {
  constructor(ring, who, opts = {}) {
    super(ring, {
      ...opts,
      questions: QUESTIONS
    });
  }

  senseState() {
    return this.ring.senseFor(this.who);
  }
}
```

The request mechanism is shared with Snake, but the fight supplies different questions and a different state format.

### Step 2: Apply the model answer

When an answer arrives:

```js
onAnswers(a) {
  if (a.action) {
    this.action = a.action.choice || 'ADVANCE';
    this.lastChoice = this.action;
    this.lastProbs = a.action.probabilities || {};
    this.fresh = true;
  }

  if (a.commit) {
    this.commitP = a.commit.noul ?? 0;
  }
}
```

The selected action is stored in `this.action`.

`fresh` is set to `true` so a punch, kick, or jump happens once per new decision instead of once per animation frame.

### Important detail about `commit`

The current code stores the commit probability:

```js
this.commitP = a.commit.noul ?? 0;
```

However, the actual action is still selected directly by:

```js
a.action.choice
```

Therefore, in the current implementation:

```text
action.choice → actual fighter action
commit.noul   → HUD probability and telemetry
```

The `commit` answer does not currently force the fighter to choose `KICK`.

### Step 3: Pace the fight according to latency

The slowest model determines the fight tempo:

```js
const lat = Math.max(
  ...agents.map(a => (a.p50 || a.lastLatency || 0) / 1000),
  0.12
);

const scale = ring.setPacing(lat);
```

`setPacing()` increases attack startup times:

```js
this.timeScale = Math.max(
  1,
  (lat * 1150) / FCFG.moves.KICK.startup
);
```

The purpose is to ensure that a slower model has enough time to see an attack and respond with `BLOCK`, `JUMP`, or `RETREAT`.

### Step 4: Advance the ring

The animation loop calls:

```js
ring.step(
  dt,
  agents[0] && agents[0].control(),
  agents[1] && agents[1].control()
);
```

The ring advances both fighters:

```js
this.advance(this.left, this.right, leftCmd, dt);
this.advance(this.right, this.left, rightCmd, dt);
```

### Step 5: Process actions

#### Block

```js
if (action === 'BLOCK') {
  me.state = 'block';
  return;
}
```

#### Punch or kick

```js
if (action === 'PUNCH' || action === 'KICK') {
  if (cmd.fresh && me.startMove(action, scale)) {
    cmd.fresh = false;
    return;
  }
}
```

`startMove()` starts the attack timeline:

```js
this.state = 'attack';
this.move = kind;
this.phase = 'startup';
this.timer = m.startup * scale;
```

#### Jump

```js
me.vy = -FCFG.jumpV;
me.y = -1;
me.state = 'jump';
```

#### Advance or retreat

```js
const dir =
  action === 'ADVANCE'
    ? me.facing
    : -me.facing;

me.x += dir * FCFG.walkSpeed * this.scale * dt;
```

### Step 6: Process attack timing

The attack moves through its phases:

```js
if (me.phase === 'startup') {
  me.phase = 'active';
  me.timer = m.active * scale;
}
else if (me.phase === 'active') {
  me.phase = 'recovery';
  me.timer = m.recovery * scale;
}
else {
  me.state = 'idle';
  me.move = null;
  me.phase = null;
}
```

An attack can hit only during the active phase:

```js
if (me.phase === 'active' && !me.hitLanded) {
  this.resolveHit(me, foe, m);
}
```

### Step 7: Resolve a hit

`resolveHit()` checks range:

```js
if (gap > m.reach * this.scale) return;
```

It checks whether the opponent is jumping:

```js
if (foe.airborne) return;
```

It checks blocking:

```js
const blocking =
  foe.state === 'block' &&
  foe.facing !== me.facing;
```

Blocked damage is reduced:

```js
const dmg = blocking
  ? m.dmg * FCFG.blockScale
  : m.dmg;
```

With `blockScale: 0.18`, a blocked attack causes only 18% of its normal damage.

Health is updated with:

```js
foe.health = Math.max(0, foe.health - dmg);
```

The hit also applies pushback, stun, animation state, and statistics.

### Step 8: End the fight

A fighter loses by knockout when health reaches zero:

```js
if (foe.health <= 0) {
  foe.state = 'ko';
  this.finish(me.side < 0 ? 'LEFT' : 'RIGHT');
}
```

If the 90-second round ends first, the fighter with more health wins:

```js
this.winner =
  this.left.health === this.right.health
    ? 'DRAW'
    : this.left.health > this.right.health
      ? 'LEFT'
      : 'RIGHT';
```

---

## 3.3 Fight output

### Canvas

The renderer displays:

- both fighters
- attack poses
- blocking poses
- jumping
- hit flashes
- sparks
- screen shake
- background and arena floor

### Health bars

`paintFrame()` updates the health bar width and percentage:

```js
$('f' + side).style.width = pct + '%';
$('h' + side).textContent = Math.round(pct) + '%';
```

### Fight statistics

The game records:

- attacks thrown
- attacks landed
- attacks blocked
- blocks performed
- damage dealt
- damage taken
- model decisions
- decisions per second
- model latency

### Verdict

The page displays one of:

```text
Laya WINS
Jev WINS
DRAW
```

The reason is either:

```text
K.O.
TIME — most health left
```

### JSON export

The fight export contains values such as:

```js
{
  tempo_scale,
  kick_startup_ms,
  elapsed_sec,
  winner,
  end_reason,
  sides: [
    {
      model,
      health_left,
      damage_dealt,
      damage_taken,
      attacks_thrown,
      attacks_landed,
      attacks_blocked_by_foe,
      times_i_blocked,
      decisions,
      decisions_per_sec,
      latency_p50_ms,
      latency_p95_ms,
      input_tokens,
      output_tokens
    }
  ]
}
```

---

# 4. Laya vs Jev API using IPO

Laya and Jev receive the same general request shape:

```json
{
  "model": "...",
  "state": { "game-specific state" },
  "questions": { "game-specific questions" }
}
```

The main difference is where inference occurs:

| Model | Browser endpoint | Processing location |
|---|---|---|
| Laya | `/api/laya` | Local Python process |
| Jev | `/api/jev` | Remote TypeSafe API |

---

## 4.1 API input

The browser sends the request from `shared/agents.js`:

```js
fetch(this.endpoint, {
  method: 'POST',
  headers: apiHeaders(),
  body: JSON.stringify({
    model: this.model,
    state: this.senseState(),
    questions: this.questions
  })
});
```

For Snake:

```text
state     → arena, snake, and nearest apple
questions → steer and sprint
```

For Kombat:

```text
state     → health, distance, fighter states, and clock
questions → action and commit
```

A visitor may also use a personal Jev key. It is sent in the `X-Jev-Key` header:

```js
if (k) h['X-Jev-Key'] = k;
```

The browser stores that key in local storage through `shared/deploy.js`.

---

## 4.2 API processing in `server.py`

### Step 1: Parse the request

The Python server only accepts the two model routes:

```python
if route not in ("/api/jev", "/api/laya"):
    return self._json(404, {"error": "not found"})
```

It reads the request body and parses JSON:

```python
incoming = json.loads(
    self.rfile.read(n) or b"{}"
)
```

### Step 2: Select the backend

```python
if route == "/api/laya":
    return self._laya(incoming)

return self._typesafe(incoming)
```

---

## 4.3 Laya API processing

The first Laya request loads the local router:

```python
_laya_router = Router(
    preload=True,
    max_loaded=3
)
```

The server then calls:

```python
res = router.predict(
    incoming.get("state"),
    incoming.get("questions"),
    model=incoming.get("laya_checkpoint")
        or LAYA_CHECKPOINT
)
```

Laya characteristics:

- no remote network call for inference
- no Jev API key required
- model runs inside the Python process
- first use can be slow while checkpoints load
- inference is protected by a lock so concurrent workers do not corrupt shared device state

The result is returned to the browser as JSON:

```python
res.setdefault(
    "model",
    incoming.get("model") or "laya"
)

return self._json(200, res)
```

---

## 4.4 Jev API processing

The server chooses either a visitor key or its configured server key:

```python
own = (self.headers.get("X-Jev-Key") or "").strip()
key = own or API_KEY
```

The server key can come from:

1. `TYPESAFE_API_KEY` environment variable
2. `.env` file
3. a visitor's `X-Jev-Key` header

The server enforces rate, concurrency, and daily limits before forwarding the request:

```python
denied = LIMITS.admit(
    self.client_ip(),
    bool(own)
)
```

The forwarded payload contains only the model, state, and questions:

```python
payload = {
    "model": incoming.get("model", MODEL),
    "state": incoming.get("state"),
    "questions": incoming.get("questions")
}
```

The request is sent to:

```python
UPSTREAM = "https://api.typesafe.ai/v1/systemone"
```

with an authorization header:

```python
headers={
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
}
```

The browser does not directly receive the server's API key. The Python process acts as a secure proxy.

---

## 4.5 API output

A successful model response is expected to contain `answers`.

A Snake response is conceptually:

```json
{
  "model": "jev-latest",
  "answers": {
    "steer": {
      "choice": "RIGHT",
      "probabilities": {
        "HARD_LEFT": 0.01,
        "LEFT": 0.04,
        "STRAIGHT": 0.15,
        "RIGHT": 0.70,
        "HARD_RIGHT": 0.10
      },
      "confidence": 0.70
    },
    "sprint": {
      "noul": 0.82
    }
  },
  "usage": {
    "input_tokens": 400,
    "output_tokens": 80
  }
}
```

A Kombat response is conceptually:

```json
{
  "model": "jev-latest",
  "answers": {
    "action": {
      "choice": "BLOCK",
      "probabilities": {
        "ADVANCE": 0.03,
        "RETREAT": 0.04,
        "PUNCH": 0.08,
        "KICK": 0.02,
        "BLOCK": 0.78,
        "JUMP": 0.05
      },
      "confidence": 0.78
    },
    "commit": {
      "noul": 0.12
    }
  },
  "usage": {
    "input_tokens": 420,
    "output_tokens": 80
  }
}
```

The browser processes the response in `ModelAgent.apply()`:

```js
const a = body.answers || {};
```

It then:

1. stores the served model name
2. records input and output tokens
3. records request latency
4. increments the decision count
5. applies the game-specific answer
6. updates the HUD

Error responses are also outputs. Examples include:

```python
return self._json(
    401,
    {"error": "no Jev API key: paste yours on the front page"}
)
```

and:

```python
return self._json(
    502,
    {"error": f"upstream failure: {exc}"}
)
```

The browser displays errors through the agent's `error` field.

---

# 5. Complete IPO flows

## Snake with Jev

```text
INPUT
  Seed
  Canvas dimensions
  Snake state
  Apple state
  steer and sprint questions
  Jev model name

PROCESSING
  Arena.senseWrapped()
  ModelAgent sends POST /api/jev
  server.py forwards to TypeSafe
  Jev returns typed answers
  ModelAgent.apply() converts answers to steering and sprint
  Arena.update() moves the snake
  Apple collision increases score and length

OUTPUT
  Canvas movement
  Score and length
  Steering probabilities
  Decisions per second
  Latency statistics
  Exported JSON
```

## Kombat with Laya

```text
INPUT
  Fighter health
  Fighter positions
  Opponent attack phase
  Distance between fighters
  action and commit questions
  Laya model

PROCESSING
  Ring.senseFor()
  ModelAgent sends POST /api/laya
  server.py calls the local Laya Router
  Laya returns a typed action
  FightAgent stores the action and marks it fresh
  Ring.advance() moves or attacks
  Attack phases progress from startup to active to recovery
  Ring.resolveHit() calculates range, blocking, damage, and stun
  Health is reduced

OUTPUT
  Fighter animation
  Health bars
  Hit and block effects
  K.O. or time result
  Fight statistics
  Exported JSON
```

---

# 6. Main design idea

The project separates the game into three layers:

## Input layer

This layer gathers:

```text
keyboard input
model selection
current game state
questions
API request data
```

## Processing layer

This layer performs:

```text
model inference
answer conversion
movement
collision detection
attack timing
blocking
health and score calculation
```

## Output layer

This layer produces:

```text
canvas rendering
HUD values
health bars
scores
probability bars
errors
JSON exports
```

The main abstraction is that a human and a model can both control the game.

For Snake, the controller produces:

```js
{
  steer,
  sprint
}
```

For Kombat, the controller produces:

```js
{
  action,
  fresh
}
```

The game engine only consumes the processed command. It does not need to know whether that command came from a human, Laya, Jev, or another model.
