/**
 * Tests for the ColumnHeader component.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ColumnHeader from "../src/components/ColumnHeader";

describe("ColumnHeader", () => {
  const defaultProps = {
    title: "To Do",
    cardCount: 5,
    onEdit: vi.fn(),
    onDelete: vi.fn(),
  };

  it("should display the column title", () => {
    render(<ColumnHeader {...defaultProps} />);

    expect(screen.getByTestId("column-title")).toHaveTextContent("To Do");
  });

  it("should display the card count badge", () => {
    render(<ColumnHeader {...defaultProps} />);

    expect(screen.getByTestId("column-card-count")).toHaveTextContent("5");
  });

  it("should render edit and delete buttons", () => {
    render(<ColumnHeader {...defaultProps} />);

    expect(screen.getByTestId("column-edit-btn")).toBeInTheDocument();
    expect(screen.getByTestId("column-delete-btn")).toBeInTheDocument();
  });

  it("should show an input field when edit button is clicked", async () => {
    const user = userEvent.setup();
    render(<ColumnHeader {...defaultProps} />);

    await user.click(screen.getByTestId("column-edit-btn"));

    const input = screen.getByTestId("column-title-input");
    expect(input).toBeInTheDocument();
    expect(input).toHaveValue("To Do");
  });

  it("should call onEdit with new title on Enter", async () => {
    const onEdit = vi.fn();
    const user = userEvent.setup();
    render(<ColumnHeader {...defaultProps} onEdit={onEdit} />);

    await user.click(screen.getByTestId("column-edit-btn"));

    const input = screen.getByTestId("column-title-input");
    await user.clear(input);
    await user.type(input, "In Progress{Enter}");

    expect(onEdit).toHaveBeenCalledWith("In Progress");
  });

  it("should cancel editing on Escape", async () => {
    const onEdit = vi.fn();
    const user = userEvent.setup();
    render(<ColumnHeader {...defaultProps} onEdit={onEdit} />);

    await user.click(screen.getByTestId("column-edit-btn"));

    const input = screen.getByTestId("column-title-input");
    await user.clear(input);
    await user.type(input, "Something{Escape}");

    // Should not call onEdit and should show the original title
    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.getByTestId("column-title")).toHaveTextContent("To Do");
  });

  it("should call onDelete when delete button is clicked", async () => {
    const onDelete = vi.fn();
    const user = userEvent.setup();
    render(<ColumnHeader {...defaultProps} onDelete={onDelete} />);

    await user.click(screen.getByTestId("column-delete-btn"));

    expect(onDelete).toHaveBeenCalledOnce();
  });

  it("should not call onEdit if title is unchanged", async () => {
    const onEdit = vi.fn();
    const user = userEvent.setup();
    render(<ColumnHeader {...defaultProps} onEdit={onEdit} />);

    await user.click(screen.getByTestId("column-edit-btn"));

    const input = screen.getByTestId("column-title-input");
    // Press Enter without changing the value
    await user.type(input, "{Enter}");

    expect(onEdit).not.toHaveBeenCalled();
  });
});
