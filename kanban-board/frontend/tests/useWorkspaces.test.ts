/**
 * Tests for the useWorkspaces and useCreateWorkspace hooks.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

// Mock the API client
vi.mock("../src/api/client", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
    defaults: { headers: { common: {} } },
  },
}));

import apiClient from "../src/api/client";
import { useWorkspaces, useCreateWorkspace } from "../src/hooks/useWorkspaces";

const mockGet = vi.mocked(apiClient.get);
const mockPost = vi.mocked(apiClient.post);

const mockWorkspaces = [
  {
    id: "1",
    name: "Team Alpha",
    slug: "team-alpha",
    owner_id: "10",
    created_at: "2024-01-01T00:00:00Z",
  },
  {
    id: "2",
    name: "Team Beta",
    slug: "team-beta",
    owner_id: "10",
    created_at: "2024-02-01T00:00:00Z",
  },
];

describe("useWorkspaces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should fetch workspaces on mount", async () => {
    mockGet.mockResolvedValueOnce({ data: mockWorkspaces });

    const { result } = renderHook(() => useWorkspaces());

    expect(result.current.loading).toBe(true);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.workspaces).toEqual(mockWorkspaces);
    expect(result.current.error).toBeNull();
    expect(mockGet).toHaveBeenCalledWith("/workspaces");
  });

  it("should set error on fetch failure", async () => {
    mockGet.mockRejectedValueOnce(new Error("Network error"));

    const { result } = renderHook(() => useWorkspaces());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.error).toBe("Failed to load workspaces.");
    expect(result.current.workspaces).toEqual([]);
  });

  it("should refetch when refetch is called", async () => {
    mockGet.mockResolvedValueOnce({ data: mockWorkspaces });

    const { result } = renderHook(() => useWorkspaces());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    const updatedWorkspaces = [
      ...mockWorkspaces,
      {
        id: "3",
        name: "Team Gamma",
        slug: "team-gamma",
        owner_id: "10",
        created_at: "2024-03-01T00:00:00Z",
      },
    ];
    mockGet.mockResolvedValueOnce({ data: updatedWorkspaces });

    act(() => {
      result.current.refetch();
    });

    await waitFor(() => {
      expect(result.current.workspaces).toEqual(updatedWorkspaces);
    });

    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

describe("useCreateWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should create a workspace successfully", async () => {
    const newWorkspace = {
      id: "3",
      name: "New Team",
      slug: "new-team",
      owner_id: "10",
      created_at: "2024-03-01T00:00:00Z",
    };
    mockPost.mockResolvedValueOnce({ data: newWorkspace });

    const { result } = renderHook(() => useCreateWorkspace());

    expect(result.current.creating).toBe(false);

    let created;
    await act(async () => {
      created = await result.current.createWorkspace({
        name: "New Team",
        slug: "new-team",
      });
    });

    expect(created).toEqual(newWorkspace);
    expect(result.current.creating).toBe(false);
    expect(result.current.error).toBeNull();
    expect(mockPost).toHaveBeenCalledWith("/workspaces", {
      name: "New Team",
      slug: "new-team",
    });
  });

  it("should set error on creation failure", async () => {
    mockPost.mockRejectedValueOnce(new Error("Conflict"));

    const { result } = renderHook(() => useCreateWorkspace());

    await act(async () => {
      try {
        await result.current.createWorkspace({
          name: "Duplicate",
          slug: "duplicate",
        });
      } catch {
        // expected
      }
    });

    expect(result.current.creating).toBe(false);
    expect(result.current.error).toBe("Conflict");
  });
});
