/**
 * TypeScript types matching the backend workspace schemas.
 */

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  owner_id: string;
  created_at: string;
}

export interface WorkspaceMember {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  joined_at: string;
}

export interface WorkspaceCreateRequest {
  name: string;
  slug: string;
}

export interface MemberAddRequest {
  email: string;
  role: "admin" | "member" | "viewer";
}
