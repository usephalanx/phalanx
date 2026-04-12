# Kanban Board SaaS — Architecture & Implementation Plan

## 1. Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Backend | FastAPI + Uvicorn | 0.115+ |
| ORM | SQLAlchemy 2.0 (async) + aiosqlite (dev) / asyncpg (prod) | 2.0+ |
| Auth | PyJWT + bcrypt | PyJWT 2.8+, bcrypt 4.1+ |
| Validation | Pydantic v2 | 2.6+ |
| Frontend | React 18 + TypeScript | 18.3+ |
| Drag & Drop | @hello-pangea/dnd (maintained fork of react-beautiful-dnd) | 16.6+ |
| HTTP Client | Axios | 1.7+ |
| Styling | Tailwind CSS | 3.4+ |
| Build Tool | Vite | 5.4+ |
| Testing (BE) | pytest + pytest-asyncio + httpx | latest |
| Testing (FE) | Vitest + React Testing Library | latest |
| Container | Docker + docker-compose | latest |

---

## 2. Data Model

### 2.1 ER Diagram (text)

```
User 1──N WorkspaceMember N──1 Workspace
                                   │
                                   1
                                   │
                                   N
                                 Board
                                   │
                                   1
                                   │
                                   N
                                Column
                                   │
                                   1
                                   │
                                   N
                                 Card
                                   │
                                   N
                                   │
                                   1
                                 User (assignee)
```

### 2.2 SQLAlchemy Models

#### `User`
```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    # relationships
    workspace_memberships: Mapped[list["WorkspaceMember"]] = relationship(back_populates="user", lazy="selectin")
    assigned_cards: Mapped[list["Card"]] = relationship(back_populates="assignee", lazy="selectin")
```

#### `Workspace`
```python
class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # relationships
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace", lazy="selectin")
    boards: Mapped[list["Board"]] = relationship(back_populates="workspace", lazy="selectin")
```

#### `WorkspaceMember`
```python
class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member")  # "owner" | "admin" | "member"
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_user"),)
    # relationships
    workspace: Mapped["Workspace"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="workspace_memberships")
```

#### `Board`
```python
class Board(Base):
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    # relationships
    workspace: Mapped["Workspace"] = relationship(back_populates="boards")
    columns: Mapped[list["Column"]] = relationship(back_populates="board", lazy="selectin", order_by="Column.position")
```

#### `Column`
```python
class Column(Base):
    __tablename__ = "columns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_id: Mapped[int] = mapped_column(Integer, ForeignKey("boards.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[float] = mapped_column(Float, nullable=False)  # fractional indexing
    color: Mapped[str | None] = mapped_column(String(7))  # hex color e.g. "#3B82F6"
    wip_limit: Mapped[int | None] = mapped_column(Integer)  # optional WIP limit
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (Index("ix_column_board_position", "board_id", "position"),)
    # relationships
    board: Mapped["Board"] = relationship(back_populates="columns")
    cards: Mapped[list["Card"]] = relationship(back_populates="column", lazy="selectin", order_by="Card.position")
```

#### `Card`
```python
class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    column_id: Mapped[int] = mapped_column(Integer, ForeignKey("columns.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[float] = mapped_column(Float, nullable=False)  # fractional indexing
    assignee_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"))
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # "low" | "medium" | "high" | "urgent"
    due_date: Mapped[datetime | None] = mapped_column(DateTime)
    labels: Mapped[str | None] = mapped_column(Text)  # JSON-encoded list of strings
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    __table_args__ = (Index("ix_card_column_position", "column_id", "position"),)
    # relationships
    column: Mapped["Column"] = relationship(back_populates="cards")
    assignee: Mapped["User | None"] = relationship(back_populates="assigned_cards")
```

### 2.3 Ordering Strategy — Fractional Indexing

Cards and Columns use `position: float` for ordering. When inserting between two items:
- `new_position = (prev_position + next_position) / 2.0`
- First item: `position = 1024.0`
- Append: `position = last_position + 1024.0`
- After ~50 repeated bisections in the same gap, trigger a full rebalance (re-assign positions as 1024, 2048, 3072…).

This avoids O(n) updates on every reorder. The rebalance endpoint is `POST /api/boards/{id}/rebalance`.

---

## 3. Auth Strategy

### 3.1 Password Hashing
- Library: `bcrypt` via `passlib[bcrypt]`
- Rounds: 12 (default)
- Functions in `backend/app/services/auth.py`:
  - `hash_password(plain: str) -> str`
  - `verify_password(plain: str, hashed: str) -> bool`

### 3.2 JWT Tokens
- Library: `PyJWT`
- Access token: 30-minute expiry, stored in memory (React state)
- Refresh token: 7-day expiry, stored in httpOnly cookie
- Payload: `{"sub": user_id, "email": email, "type": "access"|"refresh", "exp": timestamp}`
- Secret: `JWT_SECRET_KEY` env var (256-bit random)
- Algorithm: HS256

### 3.3 Auth Flow
1. `POST /api/auth/register` → create user → return access + refresh tokens
2. `POST /api/auth/login` → verify password → return access + refresh tokens
3. `POST /api/auth/refresh` → validate refresh cookie → return new access token
4. `POST /api/auth/logout` → clear refresh cookie
5. Protected routes use `Depends(get_current_user)` → decode JWT from `Authorization: Bearer <token>`

---

## 4. API Design

Base URL: `/api`

### 4.1 Auth Endpoints

| Method | Path | Request Body | Response | Status |
|--------|------|-------------|----------|--------|
| POST | `/api/auth/register` | `{email, password, display_name}` | `{access_token, user}` + Set-Cookie refresh | 201 |
| POST | `/api/auth/login` | `{email, password}` | `{access_token, user}` + Set-Cookie refresh | 200 |
| POST | `/api/auth/refresh` | (cookie) | `{access_token}` | 200 |
| POST | `/api/auth/logout` | — | `{message}` + Clear-Cookie | 200 |
| GET | `/api/auth/me` | — | `{user}` | 200 |

### 4.2 Workspace Endpoints

| Method | Path | Request Body | Response | Status |
|--------|------|-------------|----------|--------|
| GET | `/api/workspaces` | — | `[{workspace}]` | 200 |
| POST | `/api/workspaces` | `{name, slug}` | `{workspace}` | 201 |
| GET | `/api/workspaces/{slug}` | — | `{workspace, members}` | 200 |
| PATCH | `/api/workspaces/{slug}` | `{name?}` | `{workspace}` | 200 |
| DELETE | `/api/workspaces/{slug}` | — | — | 204 |
| POST | `/api/workspaces/{slug}/members` | `{email, role}` | `{member}` | 201 |
| DELETE | `/api/workspaces/{slug}/members/{user_id}` | — | — | 204 |

### 4.3 Board Endpoints

| Method | Path | Request Body | Response | Status |
|--------|------|-------------|----------|--------|
| GET | `/api/workspaces/{slug}/boards` | — | `[{board}]` | 200 |
| POST | `/api/workspaces/{slug}/boards` | `{name, description?}` | `{board}` (with default columns) | 201 |
| GET | `/api/boards/{board_id}` | — | `{board, columns: [{column, cards}]}` | 200 |
| PATCH | `/api/boards/{board_id}` | `{name?, description?}` | `{board}` | 200 |
| DELETE | `/api/boards/{board_id}` | — | — | 204 |
| POST | `/api/boards/{board_id}/rebalance` | — | `{columns, cards}` | 200 |

### 4.4 Column Endpoints

| Method | Path | Request Body | Response | Status |
|--------|------|-------------|----------|--------|
| POST | `/api/boards/{board_id}/columns` | `{name, color?, wip_limit?}` | `{column}` | 201 |
| PATCH | `/api/columns/{column_id}` | `{name?, color?, wip_limit?}` | `{column}` | 200 |
| DELETE | `/api/columns/{column_id}` | — | — | 204 |
| PUT | `/api/columns/{column_id}/reorder` | `{position}` | `{column}` | 200 |

### 4.5 Card Endpoints

| Method | Path | Request Body | Response | Status |
|--------|------|-------------|----------|--------|
| POST | `/api/columns/{column_id}/cards` | `{title, description?, assignee_id?, priority?, due_date?, labels?}` | `{card}` | 201 |
| GET | `/api/cards/{card_id}` | — | `{card}` | 200 |
| PATCH | `/api/cards/{card_id}` | `{title?, description?, assignee_id?, priority?, due_date?, labels?}` | `{card}` | 200 |
| DELETE | `/api/cards/{card_id}` | — | — | 204 |
| PUT | `/api/cards/{card_id}/move` | `{column_id, position}` | `{card}` | 200 |

The **move** endpoint handles both within-column reorder AND cross-column drag-and-drop. The frontend computes the target `column_id` and fractional `position`, then sends a single PUT.

---

## 5. File/Folder Layout

### 5.1 Backend (`kanban-board/backend/`)

```
kanban-board/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI app, lifespan, CORS, router includes
│   │   ├── config.py                  # Settings via pydantic-settings (DATABASE_URL, JWT_SECRET_KEY, etc.)
│   │   ├── database.py                # AsyncSession, engine, get_db(), init_db()
│   │   ├── models/
│   │   │   ├── __init__.py            # re-export all models
│   │   │   ├── base.py                # DeclarativeBase
│   │   │   ├── user.py                # User model
│   │   │   ├── workspace.py           # Workspace + WorkspaceMember models
│   │   │   ├── board.py               # Board model
│   │   │   ├── column.py              # Column model
│   │   │   └── card.py                # Card model
│   │   ├── schemas/
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                # RegisterRequest, LoginRequest, TokenResponse, UserResponse
│   │   │   ├── workspace.py           # WorkspaceCreate, WorkspaceResponse, MemberAdd, MemberResponse
│   │   │   ├── board.py               # BoardCreate, BoardUpdate, BoardResponse, BoardDetailResponse
│   │   │   ├── column.py              # ColumnCreate, ColumnUpdate, ColumnReorder, ColumnResponse
│   │   │   └── card.py                # CardCreate, CardUpdate, CardMove, CardResponse
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                # /api/auth/* endpoints
│   │   │   ├── workspaces.py          # /api/workspaces/* endpoints
│   │   │   ├── boards.py              # /api/workspaces/{slug}/boards/*, /api/boards/{id}/*
│   │   │   ├── columns.py             # /api/boards/{id}/columns/*, /api/columns/{id}/*
│   │   │   └── cards.py               # /api/columns/{id}/cards/*, /api/cards/{id}/*
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                # hash_password(), verify_password(), create_access_token(), create_refresh_token(), decode_token()
│   │   │   ├── position.py            # calculate_position(), rebalance_positions()
│   │   │   └── permissions.py         # get_current_user(), require_workspace_member(), require_workspace_admin()
│   │   └── seed.py                    # Idempotent seed: demo user (demo@phalanx.dev / demo1234) + sample workspace/board
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── .env.example
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── postcss.config.js
│   ├── Dockerfile
│   ├── public/
│   │   └── favicon.svg
│   └── src/
│       ├── main.tsx                   # ReactDOM.createRoot entry
│       ├── App.tsx                    # Router setup (react-router-dom v6)
│       ├── api/
│       │   ├── client.ts             # Axios instance with interceptor (attach JWT, auto-refresh)
│       │   ├── auth.ts               # login(), register(), refresh(), logout(), getMe()
│       │   ├── workspaces.ts         # CRUD functions
│       │   ├── boards.ts             # CRUD + rebalance
│       │   ├── columns.ts            # CRUD + reorder
│       │   └── cards.ts              # CRUD + move
│       ├── hooks/
│       │   ├── useAuth.ts            # AuthContext consumer hook
│       │   ├── useBoard.ts           # Fetch board detail, local state for optimistic DnD
│       │   └── useWorkspaces.ts      # Fetch user's workspaces
│       ├── context/
│       │   └── AuthContext.tsx        # AuthProvider: stores user + access_token in state, refresh logic
│       ├── pages/
│       │   ├── LoginPage.tsx
│       │   ├── RegisterPage.tsx
│       │   ├── WorkspacesPage.tsx     # List workspaces, create new
│       │   ├── BoardListPage.tsx      # Boards within a workspace
│       │   └── BoardPage.tsx          # The main Kanban board view
│       ├── components/
│       │   ├── layout/
│       │   │   ├── AppShell.tsx       # Sidebar + top bar + main content area
│       │   │   ├── Sidebar.tsx        # Workspace switcher + board list
│       │   │   └── TopBar.tsx         # User menu, logout
│       │   ├── board/
│       │   │   ├── BoardView.tsx      # DragDropContext wrapper, renders ColumnList
│       │   │   ├── ColumnList.tsx     # Horizontal flex container of Column components
│       │   │   ├── ColumnComponent.tsx # Droppable column: header + card list + add card
│       │   │   ├── CardComponent.tsx  # Draggable card: title, assignee avatar, priority badge, labels
│       │   │   ├── AddCardForm.tsx    # Inline form to create a card
│       │   │   ├── AddColumnForm.tsx  # Inline form to create a column
│       │   │   └── CardDetailModal.tsx # Full card editor: title, description, assignee, priority, due date, labels
│       │   ├── auth/
│       │   │   └── ProtectedRoute.tsx # Redirect to /login if not authenticated
│       │   └── ui/
│       │       ├── Button.tsx
│       │       ├── Input.tsx
│       │       ├── Modal.tsx
│       │       ├── Select.tsx
│       │       ├── Badge.tsx
│       │       └── Avatar.tsx
│       ├── types/
│       │   └── index.ts              # TypeScript interfaces: User, Workspace, Board, Column, Card
│       └── utils/
│           └── position.ts           # calculatePosition(prev?, next?): number — mirrors backend logic
├── tests/
│   ├── conftest.py                   # Fixtures: async db_session, client, auth_headers helper
│   ├── test_auth.py                  # Register, login, refresh, me, logout
│   ├── test_workspaces.py            # CRUD + member management
│   ├── test_boards.py                # CRUD + rebalance
│   ├── test_columns.py               # CRUD + reorder
│   ├── test_cards.py                 # CRUD + move (within-column, cross-column)
│   └── test_permissions.py           # Access control: non-member blocked, admin-only ops
├── docker-compose.yml
├── RUNNING.md
└── .env.example
```

---

## 6. Frontend Component Tree

```
<App>
  <AuthProvider>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<WorkspacesPage />} />
            <Route path="/w/:slug" element={<BoardListPage />} />
            <Route path="/w/:slug/b/:boardId" element={<BoardPage />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  </AuthProvider>
</App>

<BoardPage>
  └─ <BoardView>                    ← DragDropContext onDragEnd handler
       ├─ <ColumnList>
       │   ├─ <ColumnComponent>     ← Droppable (droppableId = column.id)
       │   │   ├─ Column header (name, WIP count, color bar, menu)
       │   │   ├─ <CardComponent />  ← Draggable (draggableId = card.id)
       │   │   ├─ <CardComponent />
       │   │   └─ <AddCardForm />
       │   ├─ <ColumnComponent> ...
       │   └─ <AddColumnForm />
       └─ <CardDetailModal />        ← Shown when a card is clicked
```

### 6.1 Drag-and-Drop Flow

1. User drags a `CardComponent` (Draggable).
2. `BoardView.onDragEnd(result)` fires with `{draggableId, source: {droppableId, index}, destination: {droppableId, index}}`.
3. **Optimistic update**: Immediately reorder local state (move card from source column to destination column at destination index).
4. **Compute position**: Call `calculatePosition(prevCard?.position, nextCard?.position)` using the cards at `destination.index - 1` and `destination.index + 1`.
5. **API call**: `PUT /api/cards/{cardId}/move` with `{column_id: destination.droppableId, position}`.
6. **Rollback on error**: If API fails, revert local state to pre-drag snapshot.

---

## 7. Key Service Functions

### `backend/app/services/auth.py`
```python
def hash_password(plain: str) -> str: ...
def verify_password(plain: str, hashed: str) -> bool: ...
def create_access_token(user_id: int, email: str) -> str: ...
def create_refresh_token(user_id: int) -> str: ...
def decode_token(token: str) -> dict: ...
```

### `backend/app/services/permissions.py`
```python
async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User: ...
async def require_workspace_member(workspace_slug: str, user: User, db: AsyncSession) -> WorkspaceMember: ...
async def require_workspace_admin(workspace_slug: str, user: User, db: AsyncSession) -> WorkspaceMember: ...
```

### `backend/app/services/position.py`
```python
def calculate_position(prev_pos: float | None, next_pos: float | None) -> float: ...
async def rebalance_positions(db: AsyncSession, items: list, parent_fk_column: str, parent_id: int) -> None: ...
```

---

## 8. Deployment Strategy

### 8.1 Docker Compose (`docker-compose.yml`)

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: kanban
      POSTGRES_USER: kanban
      POSTGRES_PASSWORD: kanban_dev_password
    ports: ["5433:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]

  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+asyncpg://kanban:kanban_dev_password@postgres:5432/kanban
      JWT_SECRET_KEY: dev-secret-key-change-in-production
      CORS_ORIGINS: http://localhost:5173
    ports: ["8000:8000"]
    depends_on: [postgres]

  frontend:
    build: ./frontend
    environment:
      VITE_API_URL: http://localhost:8000
    ports: ["5173:5173"]
    depends_on: [backend]

volumes:
  pgdata:
```

### 8.2 Backend Dockerfile (`backend/Dockerfile`)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 8.3 Frontend Dockerfile (`frontend/Dockerfile`)
```dockerfile
FROM node:20-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
```

---

## 9. Seed Data & Demo User

### `backend/app/seed.py`
```python
async def seed_demo_data(db: AsyncSession) -> None:
    """Idempotent seed — safe to run on every startup."""
    # 1. Upsert demo user: demo@phalanx.dev / demo1234
    existing = await db.execute(select(User).where(User.email == "demo@phalanx.dev"))
    if not existing.scalar_one_or_none():
        user = User(email="demo@phalanx.dev", hashed_password=hash_password("demo1234"), display_name="Demo User")
        db.add(user)
        await db.flush()

        # 2. Create sample workspace "Demo Workspace" (slug: demo)
        ws = Workspace(name="Demo Workspace", slug="demo", owner_id=user.id)
        db.add(ws)
        await db.flush()

        # 3. Add membership
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        await db.flush()

        # 4. Create sample board with default columns
        board = Board(workspace_id=ws.id, name="My First Board")
        db.add(board)
        await db.flush()

        for i, col_name in enumerate(["To Do", "In Progress", "Review", "Done"]):
            db.add(Column(board_id=board.id, name=col_name, position=(i + 1) * 1024.0))

        await db.commit()
```

Called in `main.py` lifespan:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with get_db_context() as db:
        await seed_demo_data(db)
    yield
```

---

## 10. RUNNING.md

```markdown
# Running the Kanban Board

## Quick Start (3 commands)

    git clone <repo> && cd kanban-board
    docker compose up --build
    open http://localhost:5173

## Demo Credentials

    Email:    demo@phalanx.dev
    Password: demo1234

## URLs

| Service  | URL                    |
|----------|------------------------|
| Frontend | http://localhost:5173   |
| Backend  | http://localhost:8000   |
| API Docs | http://localhost:8000/docs |

## Stopping

    docker compose down
```

---

## 11. Implementation Steps (ordered)

1. **Scaffold project structure**: Create `kanban-board/backend/` and `kanban-board/frontend/` directory trees with all `__init__.py` files and config files (`pyproject.toml`, `package.json`, `tsconfig.json`, `vite.config.ts`, `tailwind.config.js`).

2. **Backend — models**: Create all 6 model files (`base.py`, `user.py`, `workspace.py`, `board.py`, `column.py`, `card.py`) with exact schema from Section 2.2.

3. **Backend — database + config**: Create `database.py` (async engine, `get_db()`, `init_db()` that calls `Base.metadata.create_all`) and `config.py` (pydantic-settings for `DATABASE_URL`, `JWT_SECRET_KEY`, `CORS_ORIGINS`).

4. **Backend — auth service**: Implement `services/auth.py` with `hash_password`, `verify_password`, `create_access_token`, `create_refresh_token`, `decode_token`.

5. **Backend — permissions service**: Implement `services/permissions.py` with `get_current_user`, `require_workspace_member`, `require_workspace_admin`.

6. **Backend — position service**: Implement `services/position.py` with `calculate_position`, `rebalance_positions`.

7. **Backend — auth routes**: Implement `routes/auth.py` with register, login, refresh, logout, me endpoints.

8. **Backend — workspace routes**: Implement `routes/workspaces.py` with CRUD + member management.

9. **Backend — board routes**: Implement `routes/boards.py` with CRUD + rebalance. On board creation, auto-create default columns (To Do, In Progress, Review, Done).

10. **Backend — column routes**: Implement `routes/columns.py` with CRUD + reorder.

11. **Backend — card routes**: Implement `routes/cards.py` with CRUD + move endpoint.

12. **Backend — schemas**: Create all Pydantic request/response schemas in `schemas/`.

13. **Backend — main.py**: Wire up FastAPI app with lifespan (init_db + seed), CORS middleware, all routers.

14. **Seed demo data**: Implement `seed.py` per Section 9 — called in lifespan.

15. **Backend tests**: Write all test files per Section 12 below.

16. **Frontend — scaffold**: Initialize Vite + React + TypeScript project, install deps (`@hello-pangea/dnd`, `axios`, `react-router-dom`, `tailwindcss`).

17. **Frontend — types + API layer**: Create `types/index.ts` and all `api/*.ts` files.

18. **Frontend — AuthContext + hooks**: Implement `AuthContext.tsx`, `useAuth.ts`, `useBoard.ts`, `useWorkspaces.ts`.

19. **Frontend — pages**: Implement `LoginPage`, `RegisterPage`, `WorkspacesPage`, `BoardListPage`, `BoardPage`.

20. **Frontend — board components**: Implement `BoardView`, `ColumnList`, `ColumnComponent`, `CardComponent`, `AddCardForm`, `AddColumnForm`, `CardDetailModal` with drag-and-drop.

21. **Frontend — layout**: Implement `AppShell`, `Sidebar`, `TopBar`.

22. **Frontend — UI components**: Implement shared `Button`, `Input`, `Modal`, `Select`, `Badge`, `Avatar`.

23. **Docker setup**: Create `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`.

24. **RUNNING.md**: Write at repo root per Section 10.

25. **Integration test**: Full `docker compose up --build`, login with demo creds, create board, drag cards.

---

## 12. Test Strategy

### Backend Tests (`tests/`)

#### `tests/conftest.py`
- Fixture `db_session`: In-memory aiosqlite, create all tables, yield session, drop all.
- Fixture `client`: `httpx.AsyncClient` with app dependency override for `get_db`.
- Fixture `auth_headers`: Register + login demo user, return `{"Authorization": "Bearer <token>"}`.
- Fixture `seeded_workspace`: Create workspace + membership for authenticated user.
- Fixture `seeded_board`: Create board with 3 columns and 5 cards.

#### `tests/test_auth.py`
- `test_register_success` — 201, returns access_token + user with correct email
- `test_register_duplicate_email` — 409 conflict
- `test_register_invalid_email` — 422 validation error
- `test_login_success` — 200, returns access_token
- `test_login_wrong_password` — 401
- `test_login_nonexistent_user` — 401
- `test_refresh_token` — 200, returns new access_token
- `test_me_authenticated` — 200, returns user profile
- `test_me_unauthenticated` — 401

#### `tests/test_workspaces.py`
- `test_create_workspace` — 201, owner auto-added as member with role "owner"
- `test_create_workspace_duplicate_slug` — 409
- `test_list_workspaces` — returns only workspaces user is member of
- `test_get_workspace_by_slug` — 200
- `test_get_workspace_not_member` — 403
- `test_update_workspace_as_owner` — 200
- `test_delete_workspace_as_owner` — 204
- `test_delete_workspace_as_member` — 403
- `test_add_member` — 201
- `test_remove_member` — 204

#### `tests/test_boards.py`
- `test_create_board_with_default_columns` — 201, response includes 4 default columns
- `test_list_boards_in_workspace` — returns boards for workspace
- `test_get_board_detail` — 200, includes columns with cards ordered by position
- `test_update_board` — 200
- `test_delete_board` — 204, cascades to columns + cards
- `test_rebalance_board` — positions re-normalized to 1024 increments

#### `tests/test_columns.py`
- `test_create_column` — 201, appended at end with correct position
- `test_update_column` — 200
- `test_delete_column` — 204
- `test_reorder_column` — position updated, other columns unchanged
- `test_wip_limit_enforced` — 400 when adding card to column at WIP limit

#### `tests/test_cards.py`
- `test_create_card` — 201, correct column_id and position
- `test_get_card` — 200
- `test_update_card_title` — 200
- `test_update_card_assignee` — 200
- `test_delete_card` — 204
- `test_move_card_within_column` — position changed, column_id unchanged
- `test_move_card_cross_column` — column_id changed to destination
- `test_move_card_to_top` — position < first card's position
- `test_move_card_to_bottom` — position > last card's position
- `test_move_card_between_two` — position is midpoint of neighbors

#### `tests/test_permissions.py`
- `test_non_member_cannot_access_workspace_boards` — 403
- `test_member_can_read_board` — 200
- `test_non_member_cannot_move_card` — 403
- `test_admin_can_delete_board` — 204
- `test_member_cannot_delete_workspace` — 403

---

## 13. Acceptance Criteria

1. `docker compose up --build` starts all 3 services (postgres, backend, frontend) without errors.
2. `http://localhost:5173` loads the login page.
3. Login with `demo@phalanx.dev` / `demo1234` succeeds and redirects to the workspaces page.
4. "Demo Workspace" appears in the workspace list with "My First Board" inside it.
5. Opening "My First Board" shows 4 columns: "To Do", "In Progress", "Review", "Done".
6. Creating a new card in "To Do" via the inline form adds it to the bottom of the column.
7. Dragging a card from "To Do" to "In Progress" persists after page refresh.
8. Dragging a card to a new position within the same column persists after page refresh.
9. `pytest --cov=app --cov-fail-under=80 -x -q` passes with ≥80% coverage.
10. `POST /api/auth/register` with a duplicate email returns 409.
11. All protected endpoints return 401 without a valid JWT.
12. Non-workspace-members receive 403 on workspace/board/card endpoints.
13. Board rebalance endpoint (`POST /api/boards/{id}/rebalance`) re-normalizes all column and card positions to 1024-increment spacing.
14. Card move endpoint accepts `{column_id, position}` and correctly handles both within-column and cross-column moves.
15. RUNNING.md exists at repo root with ≤3 commands to start the app.

---

## 14. Edge Cases

1. **Concurrent drag-and-drop**: Two users drag cards to the same position simultaneously — fractional indexing ensures both get unique positions (no collision), but the final order may differ from what each user saw optimistically. The rebalance endpoint fixes drift.
2. **Fractional position exhaustion**: After ~50 bisections in the same gap, positions differ by < 0.001. The rebalance endpoint must be called (can be triggered automatically when `abs(prev - next) < 0.01`).
3. **Deleting a column with cards**: CASCADE delete removes all cards. Frontend must confirm with the user.
4. **WIP limit enforcement**: When `wip_limit` is set and column is full, the move endpoint returns 400. Frontend shows the column as "full" visually.
5. **JWT expiry during drag**: If the access token expires mid-drag, the Axios interceptor auto-refreshes before retrying the move API call. Optimistic state remains until the retry resolves.
6. **Empty board**: Board with 0 columns — "Add Column" form is the only visible element.
7. **Workspace slug collision**: Two users try to create workspaces with the same slug — unique constraint returns 409.
8. **Self-removal from workspace**: Owner cannot remove themselves; endpoint returns 400.
9. **Refresh token rotation**: Each refresh call issues a new refresh token and invalidates the old one (prevents token reuse after theft).
10. **Very long card titles**: Frontend truncates at 200 chars with ellipsis; backend enforces `String(200)`.

---

## 15. Estimated Complexity

**7 / 10** — Full-stack SaaS with auth, RBAC, real-time-ish drag-and-drop ordering, fractional indexing, and multi-entity data model. No WebSockets (MVP), no file uploads, no billing — keeps it below 8.
