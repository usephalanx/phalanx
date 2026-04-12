/**
 * Workspace dashboard page — shows boards grid for selected workspace,
 * create-board modal, and workspace member list sidebar with invite button.
 */

import { type FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import apiClient from "../api/client";
import Modal from "../components/Modal";
import { useBoards, useCreateBoard } from "../hooks/useBoards";
import {
  useWorkspaceMembers,
  useInviteMember,
} from "../hooks/useWorkspaceMembers";
import type { Workspace } from "../types/workspace";

/**
 * Page that displays a single workspace dashboard with boards grid,
 * member sidebar, create-board modal, and invite modal.
 */
export default function WorkspaceDetailPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [wsLoading, setWsLoading] = useState(true);
  const [wsError, setWsError] = useState<string | null>(null);

  const { boards, loading: boardsLoading, error: boardsError, refetch: refetchBoards } = useBoards(id);
  const { members, loading: membersLoading, refetch: refetchMembers } = useWorkspaceMembers(id);
  const { createBoard, creating: creatingBoard } = useCreateBoard(id);
  const { inviteMember, inviting } = useInviteMember(id);

  // Board creation modal state
  const [boardModalOpen, setBoardModalOpen] = useState(false);
  const [newBoardName, setNewBoardName] = useState("");
  const [newBoardDescription, setNewBoardDescription] = useState("");
  const [boardCreateError, setBoardCreateError] = useState<string | null>(null);

  // Invite modal state
  const [inviteModalOpen, setInviteModalOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"admin" | "member" | "viewer">("member");
  const [inviteError, setInviteError] = useState<string | null>(null);

  // Fetch workspace details
  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function fetchWorkspace(): Promise<void> {
      setWsLoading(true);
      setWsError(null);
      try {
        const response = await apiClient.get<Workspace>(`/workspaces/${id}`);
        if (!cancelled) {
          setWorkspace(response.data);
        }
      } catch {
        if (!cancelled) {
          setWsError("Failed to load workspace.");
        }
      } finally {
        if (!cancelled) {
          setWsLoading(false);
        }
      }
    }

    void fetchWorkspace();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Board modal handlers
  function openBoardModal(): void {
    setNewBoardName("");
    setNewBoardDescription("");
    setBoardCreateError(null);
    setBoardModalOpen(true);
  }

  function closeBoardModal(): void {
    setBoardModalOpen(false);
  }

  async function handleCreateBoard(e: FormEvent): Promise<void> {
    e.preventDefault();
    const trimmedName = newBoardName.trim();
    if (!trimmedName) return;

    setBoardCreateError(null);
    try {
      await createBoard({
        name: trimmedName,
        description: newBoardDescription.trim() || undefined,
      });
      closeBoardModal();
      refetchBoards();
    } catch {
      setBoardCreateError("Failed to create board.");
    }
  }

  // Invite modal handlers
  function openInviteModal(): void {
    setInviteEmail("");
    setInviteRole("member");
    setInviteError(null);
    setInviteModalOpen(true);
  }

  function closeInviteModal(): void {
    setInviteModalOpen(false);
  }

  async function handleInvite(e: FormEvent): Promise<void> {
    e.preventDefault();
    const trimmedEmail = inviteEmail.trim();
    if (!trimmedEmail) return;

    setInviteError(null);
    try {
      await inviteMember({ email: trimmedEmail, role: inviteRole });
      closeInviteModal();
      refetchMembers();
    } catch {
      setInviteError("Failed to invite member. The user may not exist or is already a member.");
    }
  }

  if (wsLoading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary-500 border-t-transparent" />
      </div>
    );
  }

  if (wsError) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          {wsError}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      {/* Breadcrumb */}
      <div className="mb-6 flex items-center gap-2 text-sm">
        <Link to="/workspaces" className="text-gray-500 hover:text-gray-700">
          Workspaces
        </Link>
        <span className="text-gray-300">/</span>
        <span className="font-medium text-gray-900">{workspace?.name}</span>
      </div>

      <div className="flex flex-col gap-8 lg:flex-row">
        {/* Main content: boards grid */}
        <div className="flex-1">
          {/* Section header with create button */}
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-800">Boards</h2>
            <button
              type="button"
              onClick={openBoardModal}
              className="btn-primary flex items-center gap-2 text-sm"
            >
              <svg
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              New Board
            </button>
          </div>

          {boardsLoading && (
            <div className="flex justify-center py-8">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary-500 border-t-transparent" />
            </div>
          )}

          {boardsError && (
            <div className="rounded-md bg-red-50 p-4 text-sm text-red-700">
              {boardsError}
            </div>
          )}

          {!boardsLoading && !boardsError && boards.length === 0 && (
            <p className="py-8 text-center text-gray-500">
              No boards yet. Create one to get started.
            </p>
          )}

          {!boardsLoading && !boardsError && boards.length > 0 && (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {boards.map((board) => (
                <Link
                  key={board.id}
                  to={`/boards/${board.id}`}
                  className="card block"
                >
                  <h3 className="text-lg font-semibold text-gray-900">
                    {board.name}
                  </h3>
                  {board.description && (
                    <p className="mt-1 line-clamp-2 text-sm text-gray-500">
                      {board.description}
                    </p>
                  )}
                  <p className="mt-2 text-xs text-gray-400">
                    Created {new Date(board.created_at).toLocaleDateString()}
                  </p>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Sidebar: workspace members */}
        <aside className="w-full shrink-0 lg:w-72">
          <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-gray-800">Members</h3>
              <button
                type="button"
                onClick={openInviteModal}
                className="rounded-md bg-primary-50 px-2.5 py-1 text-xs font-medium text-primary-700 hover:bg-primary-100"
                aria-label="Invite member"
              >
                Invite
              </button>
            </div>

            {membersLoading && (
              <div className="flex justify-center py-4">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary-500 border-t-transparent" />
              </div>
            )}

            {!membersLoading && members.length === 0 && (
              <p className="text-xs text-gray-400">No members found.</p>
            )}

            {!membersLoading && members.length > 0 && (
              <ul className="space-y-2" data-testid="member-list">
                {members.map((member) => (
                  <li
                    key={member.user_id}
                    className="flex items-center gap-2"
                  >
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-gray-200 text-xs font-medium text-gray-600">
                      {(member.display_name || member.email).charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-gray-800">
                        {member.display_name || member.email}
                      </p>
                      <p className="truncate text-xs text-gray-400">
                        {member.role}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>

      {/* Create Board Modal */}
      <Modal open={boardModalOpen} onClose={closeBoardModal} title="Create Board">
        <form onSubmit={handleCreateBoard}>
          <label
            htmlFor="board-name"
            className="block text-sm font-medium text-gray-700"
          >
            Board Name
          </label>
          <input
            id="board-name"
            type="text"
            value={newBoardName}
            onChange={(e) => setNewBoardName(e.target.value)}
            className="input"
            placeholder="Sprint Board"
            required
            autoFocus
            maxLength={200}
          />

          <label
            htmlFor="board-description"
            className="mt-4 block text-sm font-medium text-gray-700"
          >
            Description (optional)
          </label>
          <textarea
            id="board-description"
            value={newBoardDescription}
            onChange={(e) => setNewBoardDescription(e.target.value)}
            className="input"
            placeholder="A brief description of this board..."
            rows={3}
          />

          {boardCreateError && (
            <p className="mt-2 text-sm text-red-600">{boardCreateError}</p>
          )}

          <div className="mt-6 flex justify-end gap-3">
            <button
              type="button"
              onClick={closeBoardModal}
              className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={creatingBoard || !newBoardName.trim()}
              className="btn-primary text-sm font-medium"
            >
              {creatingBoard ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </Modal>

      {/* Invite Member Modal */}
      <Modal open={inviteModalOpen} onClose={closeInviteModal} title="Invite Member">
        <form onSubmit={handleInvite}>
          <label
            htmlFor="invite-email"
            className="block text-sm font-medium text-gray-700"
          >
            Email Address
          </label>
          <input
            id="invite-email"
            type="email"
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            className="input"
            placeholder="colleague@example.com"
            required
            autoFocus
          />

          <label
            htmlFor="invite-role"
            className="mt-4 block text-sm font-medium text-gray-700"
          >
            Role
          </label>
          <select
            id="invite-role"
            value={inviteRole}
            onChange={(e) =>
              setInviteRole(e.target.value as "admin" | "member" | "viewer")
            }
            className="input"
          >
            <option value="member">Member</option>
            <option value="admin">Admin</option>
            <option value="viewer">Viewer</option>
          </select>

          {inviteError && (
            <p className="mt-2 text-sm text-red-600">{inviteError}</p>
          )}

          <div className="mt-6 flex justify-end gap-3">
            <button
              type="button"
              onClick={closeInviteModal}
              className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={inviting || !inviteEmail.trim()}
              className="btn-primary text-sm font-medium"
            >
              {inviting ? "Inviting..." : "Send Invite"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
