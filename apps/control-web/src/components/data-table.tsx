import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { useState } from "react";
import { cn } from "../lib";
import { Empty } from "../ui";

function columnClass(meta: unknown): string | undefined {
  if (!meta || typeof meta !== "object" || !("className" in meta)) return undefined;
  const className = meta.className;
  return typeof className === "string" ? className : undefined;
}

export function DataTable<T>({
  columns,
  data,
  empty = "No records found",
}: {
  columns: ColumnDef<T>[];
  data: T[];
  empty?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  if (data.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-slate-800">
      <table className="w-full table-fixed border-collapse text-left text-sm">
        <thead className="bg-slate-900/90 text-xs uppercase tracking-wider text-slate-400">
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th
                  className={cn(
                    "px-3 py-3 font-medium",
                    columnClass(header.column.columnDef.meta),
                  )}
                  key={header.id}
                >
                  {header.isPlaceholder ? null : (
                    <button
                      className="inline-flex max-w-full items-center gap-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {header.column.getIsSorted() === "asc" ? (
                        <ArrowUp size={13} />
                      ) : header.column.getIsSorted() === "desc" ? (
                        <ArrowDown size={13} />
                      ) : header.column.getCanSort() ? (
                        <ChevronsUpDown size={13} />
                      ) : null}
                    </button>
                  )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              className="border-t border-slate-800/80 hover:bg-slate-900/60"
              key={row.id}
            >
              {row.getVisibleCells().map((cell) => (
                <td
                  className={cn(
                    "overflow-hidden px-3 py-3 align-top text-slate-200",
                    columnClass(cell.column.columnDef.meta),
                  )}
                  key={cell.id}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
