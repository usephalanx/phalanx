/**
 * TypeScript types matching the backend board, column, and card schemas.
 */

export interface Board {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface Column {
  id: string;
  board_id: string;
  name: string;
  color: string | null;
  wip_limit: number | null;
  position: number;
  created_at?: string;
}

export interface Card {
  id: string;
  column_id: string;
  title: string;
  description: string | null;
  position: number;
  assignee_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ColumnWithCards extends Column {
  cards: Card[];
}

export interface BoardDetail extends Board {
  columns: ColumnWithCards[];
}

export interface BoardCreateRequest {
  name: string;
  description?: string;
}

export interface ColumnCreateRequest {
  name: string;
  color?: string | null;
  wip_limit?: number | null;
}

export interface ColumnUpdateRequest {
  name?: string;
  color?: string | null;
  wip_limit?: number | null;
}

export interface CardCreateRequest {
  title: string;
  description?: string | null;
  assignee_id?: string | null;
}
