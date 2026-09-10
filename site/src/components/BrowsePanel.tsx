import { useEffect, useMemo, useState } from "react";

import { getTaxonomyTree } from "../api/client";
import type { TaxonomyClass, TaxonomyNode, TaxonomyTerm } from "../api/client";
import { ConceptDetailsLink } from "./ConceptDetailsLink";

type BrowsePanelProps = {
  axis: TaxonomyClass | null;
  selectedTerms: TaxonomyTerm[];
  onAdd: (term: TaxonomyTerm) => void;
  onRemove: (uri: string) => void;
  onClose: () => void;
};

const AXIS_LABELS: Record<TaxonomyClass, string> = {
  state: "States & conditions",
  condition: "States & conditions",
  intervention: "Interventions",
  outcome: "Outcomes",
  region: "Regions",
};

// Return true if the node itself matches the filter string (label, altLabel,
// or definition) -- not descendants. Used to decide whether to force-show a
// node's whole subtree unfiltered.
function selfMatches(node: TaxonomyNode, q: string): boolean {
  if (!q) return true;
  return (
    node.label.toLowerCase().includes(q) ||
    Boolean(node.definition?.toLowerCase().includes(q)) ||
    Boolean(node.altLabels?.some((alt) => alt.toLowerCase().includes(q)))
  );
}

// Return true if the node or any descendant matches the filter string.
function nodeMatches(node: TaxonomyNode, q: string): boolean {
  if (!q) return true;
  if (selfMatches(node, q)) return true;
  return node.children.some((c) => nodeMatches(c, q));
}

type TreeNodeProps = {
  axis: TaxonomyClass;
  node: TaxonomyNode;
  filter: string;
  expanded: Set<string>;
  selectedUris: Set<string>;
  onToggle: (uri: string) => void;
  onAdd: (term: TaxonomyTerm) => void;
  onRemove: (uri: string) => void;
  // True once an ancestor (or this node) matched the filter directly --
  // once that happens, show the whole subtree unfiltered so users can
  // explore "all children of X" rather than only children whose own text
  // happens to contain the search string.
  forceAll?: boolean;
};

function TreeNode({
  axis,
  node,
  filter,
  expanded,
  selectedUris,
  onToggle,
  onAdd,
  onRemove,
  forceAll = false,
}: TreeNodeProps) {
  const selfMatch = selfMatches(node, filter);
  const showAllChildren = forceAll || selfMatch;
  const visibleChildren = filter && !showAllChildren
    ? node.children.filter((c) => nodeMatches(c, filter))
    : node.children;

  const hasChildren = visibleChildren.length > 0;
  const isExpanded = expanded.has(node.uri) || (filter.length > 0 && hasChildren);
  const isSelected = selectedUris.has(node.uri);

  const dimmed = filter.length > 0 && !selfMatch;
  const isCollection = node.collection === true;

  return (
    <div className="tree-node">
      <div className={`tree-row${isCollection ? " collection-header" : ""}${isSelected ? " selected" : ""}${dimmed ? " dimmed" : ""}`}>
        <button
          type="button"
          className={`tree-toggle-btn${hasChildren ? "" : " tree-toggle-leaf"}`}
          onClick={() => hasChildren && onToggle(node.uri)}
          aria-label={hasChildren ? `${isExpanded ? "Collapse" : "Expand"} ${node.label}` : undefined}
          tabIndex={hasChildren ? 0 : -1}
        >
          {hasChildren ? (isExpanded ? "▼" : "▶") : ""}
        </button>
        <span
          className={`tree-label${hasChildren ? " tree-label-parent" : ""}${isCollection ? " tree-label-collection" : ""}`}
          title={node.label}
        >
          {node.label}
        </span>
        {!isCollection && (axis === "state" || axis === "condition" || axis === "intervention") ? (
          <ConceptDetailsLink
            uri={node.uri}
            label={node.label}
            expectedType={axis === "intervention" ? "intervention" : "state"}
          />
        ) : null}
        {!isCollection && (isSelected ? (
          <button
            type="button"
            className="browse-remove-btn"
            onClick={() => onRemove(node.uri)}
          >
            Added ×
          </button>
        ) : (
          <button
            type="button"
            className="browse-add-btn"
            onClick={() =>
              onAdd({ uri: node.uri, label: node.label, definition: node.definition })
            }
          >
            + Add
          </button>
        ))}
      </div>
      {hasChildren && isExpanded && (
        <div className="tree-children">
          {visibleChildren.map((child) => (
            <TreeNode
              key={child.uri}
              axis={axis}
              node={child}
              filter={filter}
              expanded={expanded}
              selectedUris={selectedUris}
              onToggle={onToggle}
              onAdd={onAdd}
              onRemove={onRemove}
              forceAll={showAllChildren}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function BrowsePanel({ axis, selectedTerms, onAdd, onRemove, onClose }: BrowsePanelProps) {
  const [tree, setTree] = useState<TaxonomyNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  // Expanded state: all nodes collapsed by default, including top-level
  // branches -- a user should see the list of top-level categories first
  // and expand only the ones they want, not land on a pre-expanded tree.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Fetch tree whenever the axis changes
  useEffect(() => {
    if (!axis) return;
    setTree([]);
    setFilter("");
    setError(null);
    setLoading(true);
    getTaxonomyTree(axis)
      .then((nodes) => {
        setTree(nodes);
        setExpanded(new Set());
        setLoading(false);
      })
      .catch(() => {
        setError("Could not load taxonomy. Is the API running?");
        setLoading(false);
      });
  }, [axis]);

  function toggleExpand(uri: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  }

  const selectedUris = useMemo(
    () => new Set(selectedTerms.map((t) => t.uri)),
    [selectedTerms],
  );

  const filterLower = filter.toLowerCase();
  const visibleRoots = filterLower
    ? tree.filter((n) => nodeMatches(n, filterLower))
    : tree;

  return (
    <div className={`browse-panel${axis !== null ? " open" : ""}`} data-axis={axis === "condition" ? "state" : axis ?? undefined} aria-hidden={axis === null}>
      <div className="browse-panel-header">
        <h3>{axis ? `Browse: ${AXIS_LABELS[axis]}` : "Browse"}</h3>
        <button type="button" className="browse-close-btn" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </div>

      <div className="browse-filter-wrap">
        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter…"
          aria-label="Filter taxonomy"
        />
      </div>

      <div className="browse-body">
        {loading && <p className="browse-status">Loading…</p>}
        {error && <p className="browse-status" style={{ color: "#8b2525" }}>{error}</p>}
        {!loading && !error && visibleRoots.length === 0 && filterLower && (
          <p className="browse-status">No terms match "{filter}".</p>
        )}
        {!loading && !error && axis && visibleRoots.map((node) => (
          <TreeNode
            key={node.uri}
            axis={axis}
            node={node}
            filter={filterLower}
            expanded={expanded}
            selectedUris={selectedUris}
            onToggle={toggleExpand}
            onAdd={onAdd}
            onRemove={onRemove}
          />
        ))}
      </div>
    </div>
  );
}
