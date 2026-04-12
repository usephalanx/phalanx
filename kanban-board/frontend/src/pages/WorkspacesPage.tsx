/**
 * Workspaces listing page — shows all workspaces the user belongs to
 * as cards, with a create-workspace modal.
 */

import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import Modal from "../components/Modal";
import { useWorkspaces, useCreateWorkspace } from "../hooks/useWorkspaces";

/**
 * Generate a URL-friendly slug from a workspace name.
 */
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/**
 * Page that lists all workspaces the current user is a member of.
 * Includes a button to create new workspaces via a modal dialog.
 */
export default function WorkspacesPage(): JSX.Element {
  const { workspaces, loading, error, refetch } = useWorkspaces();
  const { createWorkspace, creating } = useCreateWorkspace();

  const [modalOpen, setModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  function openModal(): void {
    setNewName("");
    setCreateError(null);
    setModalOpen(true);
  }

  function closeModal(): void {
    setModalOpen(false);
  }

  async function handleCreate(e: FormEvent): Promise<void> {
    e.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed) return;

    setCreateError(null);
    try {
      await createWorkspace({ name: trimmed, slug: slugify(trimmed) });
      closeModal();
      refetch();
    } catch {
      setCreateError("Failed to create workspace. The name or slug may already be taken.");
    }
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-8">
      {/* Header with title and create button */}
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Workspaces</h1>
        <button
          type="button"
          onClick={openModal}
          className="btn-primary flex items-center gap-2"
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
          New Workspace
        </button>
      </div>

      {loading && (
        <div className="flex justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary-500 border-t-transparent" />
        </div>
      )}

      {error && (
        <div className="rounded-md bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {!loading && !error && workspaces.length === 0 && (
        <p className="text-center text-gray-500">
          No workspaces yet. Create one to get started.
        </p>
      )}

      {!loading && !error && workspaces.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {workspaces.map((ws) => (
            <Link
              key={ws.id}
              to={`/workspaces/${ws.id}`}
              className="card block"
            >
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary-100 text-primary-700 font-bold text-lg">
                  {ws.name.charAt(0).toUpperCase()}
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-gray-900">
                    {ws.name}
                  </h2>
                  <p className="text-xs text-gray-400">
                    Created {new Date(ws.created_at).toLocaleDateString()}
                  </p>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {/* Create Workspace Modal */}
      <Modal open={modalOpen} onClose={closeModal} title="Create Workspace">
        <form onSubmit={handleCreate}>
          <label
            htmlFor="workspace-name"
            className="block text-sm font-medium text-gray-700"
          >
            Workspace Name
          </label>
          <input
            id="workspace-name"
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="input"
            placeholder="My Workspace"
            required
            autoFocus
            maxLength={200}
          />
          {newName.trim() && (
            <p className="mt-1 text-xs text-gray-400">
              Slug: {slugify(newName.trim())}
            </p>
          )}

          {createError && (
            <p className="mt-2 text-sm text-red-600">{createError}</p>
          )}

          <div className="mt-6 flex justify-end gap-3">
            <button
              type="button"
              onClick={closeModal}
              className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={creating || !newName.trim()}
              className="btn-primary text-sm font-medium"
            >
              {creating ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
