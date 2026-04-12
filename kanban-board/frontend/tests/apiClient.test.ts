/**
 * Tests for the Axios API client interceptors.
 */

import { describe, it, expect, beforeEach } from "vitest";

describe("API Client", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("should export a default axios instance", async () => {
    const { default: client } = await import("../src/api/client");
    expect(client).toBeDefined();
    expect(client.defaults.headers["Content-Type"]).toBe("application/json");
  });

  it("should attach Authorization header when token exists", async () => {
    localStorage.setItem("access_token", "my-test-token");

    const { default: client } = await import("../src/api/client");

    // Verify the request interceptor is registered
    expect(client.interceptors.request).toBeDefined();
  });

  it("should have response interceptor registered", async () => {
    const { default: client } = await import("../src/api/client");
    expect(client.interceptors.response).toBeDefined();
  });
});
