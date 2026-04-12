/**
 * Tests for the useBoards and useCreateBoard hooks.
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
import { useBoards, useCreateBoard } from "../src/hooks/useBoards";

const mockGet = vi.mocked(apiClient.get);
const mockPost = vi.mocked(apiClient.post);

const mockBoards = [
  {
    id: "1",
    workspace_id: "10",
    name: "Sprint Board",
    description: "Current sprint tasks",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
  },
  {
    id: "2",
    workspace_id: "10",
    name: "Backlog",
    description: null,
    created_at: "2024-02-01T00:00:00Z",
    updated_at: "2024-02-01T00:00:00Z",
  },
];

describe("useBoards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should fetch boards for a workspace", async () => {
    mockGet.mockResolvedValueOnce({ data: mockBoards });

    const { result } = renderHook(() => useBoards("10"));

    expect(result.current.loading).toBe(true);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.boards).toEqual(mockBoards);
    expect(result.current.error).toBeNull();
    expect(mockGet).toHaveBeenCalledWith("/workspaces/10/boards");
  });

  it("should not fetch if workspaceId is undefined", async () => {
    const { result } = renderHook(() => useBoards(undefined));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.boards).toEqual([]);
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("should set error on fetch failure", async () => {
    mockGet.mockRejectedValueOnce(new Error("Network error"));

    const { result } = renderHook(() => useBoards("10"));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.error).toBe("Failed to load boards.");
    expect(result.current.boards).toEqual([]);
  });

  it("should refetch when refetch is called", async () => {
    mockGet.mockResolvedValueOnce({ data: mockBoards });

    const { result } = renderHook(() => useBoards("10"));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    const updatedBoards = [
      ...mockBoards,
      {
        id: "3",
        workspace_id: "10",
        name: "Design",
        description: "Design tasks",
        created_at: "2024-03-01T00:00:00Z",
        updated_at: "2024-03-01T00:00:00Z",
      },
    ];
    mockGet.mockResolvedValueOnce({ data: updatedBoards });

    act(() => {
      result.current.refetch();
    });

    await waitFor(() => {
      expect(result.current.boards).toEqual(updatedBoards);
    });

    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

describe("useCreateBoard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should create a board successfully", async () => {
    const newBoard = {
      id: "3",
      workspace_id: "10",
      name: "New Board",
      description: "A new board",
      created_at: "2024-03-01T00:00:00Z",
      updated_at: "2024-03-01T00:00:00Z",
    };
    mockPost.mockResolvedValueOnce({ data: newBoard });

    const { result } = renderHook(() => useCreateBoard("10"));

    let created;
    await act(async () => {
      created = await result.current.createBoard({
        name: "New Board",
        description: "A new board",
      });
    });

    expect(created).toEqual(newBoard);
    expect(result.current.creating).toBe(false);
    expect(result.current.error).toBeNull();
    expect(mockPost).toHaveBeenCalledWith("/workspaces/10/boards", {
      name: "New Board",
      description: "A new board",
    });
  });

  it("should throw if workspaceId is undefined", async () => {
    const { result } = renderHook(() => useCreateBoard(undefined));

    await act(async () => {
      try {
        await result.current.createBoard({ name: "Test" });
      } catch (err) {
        expect(err).toBeInstanceOf(Error);
        expect((err as Error).message).toBe(
          "Workspace ID is required to create a board.",
        );
      }
    });
  });

  it("should set error on creation failure", async () => {
    mockPost.mockRejectedValueOnce(new Error("Server error"));

    const { result } = renderHook(() => useCreateBoard("10"));

    await act(async () => {
      try {
        await result.current.createBoard({ name: "Fail" });
      } catch {
        // expected
      }
    });

    expect(result.current.creating).toBe(false);
    expect(result.current.error).toBe("Server error");
  });
});
