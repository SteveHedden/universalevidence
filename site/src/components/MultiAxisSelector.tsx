import { useEffect, useId, useState } from "react";

import { searchTaxonomy } from "../api/client";
import type { TaxonomyClass, TaxonomyTerm } from "../api/client";
import { ConceptDetailsLink } from "./ConceptDetailsLink";

type MultiAxisSelectorProps = {
  axis: TaxonomyClass;
  label: string;
  placeholder: string;
  terms: TaxonomyTerm[];
  logic: "and" | "or";
  onAdd: (term: TaxonomyTerm) => void;
  onRemove: (uri: string) => void;
  onLogicChange: (logic: "and" | "or") => void;
  onBrowseOpen: () => void;
  browseActive: boolean;
  hideLabel?: boolean;
  // Optional: fired on every keystroke, used for free-text fallback (e.g. region country text)
  onInputChange?: (value: string) => void;
  controlledInputValue?: string;
};

const DEBOUNCE_MS = 250;

export function MultiAxisSelector({
  axis,
  label,
  placeholder,
  terms,
  logic,
  onAdd,
  onRemove,
  onLogicChange,
  onBrowseOpen,
  browseActive,
  hideLabel = false,
  onInputChange,
  controlledInputValue,
}: MultiAxisSelectorProps) {
  const inputId = useId();
  const listboxId = `${inputId}-listbox`;
  const [inputValue, setInputValue] = useState("");
  const [options, setOptions] = useState<TaxonomyTerm[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const currentInputValue = controlledInputValue ?? inputValue;

  useEffect(() => {
    const query = currentInputValue.trim();
    if (query.length < 2) {
      setOptions([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setOpen(true);
    const timer = window.setTimeout(() => {
      searchTaxonomy(axis, query)
        .then((results) => {
          if (!cancelled) {
            setOptions(results.filter((r) => !terms.some((t) => t.uri === r.uri)));
            setLoading(false);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setOptions([]);
            setLoading(false);
          }
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [axis, currentInputValue, terms]);

  function handleSelect(term: TaxonomyTerm) {
    onAdd(term);
    setInputValue("");
    onInputChange?.("");
    setOptions([]);
    setOpen(false);
  }

  return (
    <div className="axis-selector" data-axis={axis === "condition" ? "state" : axis}>
      <label htmlFor={inputId} className={hideLabel ? "sr-only" : undefined}>{label}</label>
      <div className="chip-input-wrap">
        {terms.length > 0 && (
          <div className="chips-row">
            {terms.map((t) => (
              <span key={t.uri} className="chip">
                <span className="chip-label">{t.label}</span>
                {(axis === "state" || axis === "condition" || axis === "intervention") ? (
                  <ConceptDetailsLink
                    uri={t.uri}
                    label={t.label}
                    expectedType={axis === "intervention" ? "intervention" : "state"}
                    collection={t.collection}
                  />
                ) : null}
                <button
                  type="button"
                  className="chip-remove"
                  onClick={() => onRemove(t.uri)}
                  aria-label={`Remove ${t.label}`}
                  title={`Remove ${t.label}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="input-row">
          <input
            id={inputId}
            role="combobox"
            aria-autocomplete="list"
            aria-controls={listboxId}
            aria-expanded={open && (loading || options.length > 0)}
            autoComplete="off"
            value={currentInputValue}
            placeholder={terms.length === 0 ? placeholder : "Add another…"}
            onChange={(e) => {
              setInputValue(e.target.value);
              onInputChange?.(e.target.value);
            }}
            onFocus={() => currentInputValue.length >= 2 && setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
          />
          <button
            type="button"
            className={`browse-btn${browseActive ? " active" : ""}`}
            onClick={onBrowseOpen}
            aria-label={`Browse ${label}`}
          >
            Browse
          </button>
        </div>
      </div>

      {terms.length >= 2 && (
        <div className="logic-toggle">
          <button
            type="button"
            className={logic === "or" ? "active" : ""}
            onClick={() => onLogicChange("or")}
          >
            OR
          </button>
          <button
            type="button"
            className={logic === "and" ? "active" : ""}
            onClick={() => onLogicChange("and")}
          >
            AND
          </button>
          <span className="logic-toggle-label">within this axis</span>
        </div>
      )}

      {open && (loading || options.length > 0) ? (
        <div className="option-popover" id={listboxId}>
          {loading ? <div className="option-status">Loading…</div> : null}
          {options.length > 0 ? (
            <ul role="listbox" aria-label={`${label} options`}>
              {options.map((term) => (
                <li key={term.uri} role="option" aria-selected={false}>
                  <button type="button" onMouseDown={() => handleSelect(term)}>
                    <strong>{term.label}</strong>
                    {term.definition ? <span>{term.definition}</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
