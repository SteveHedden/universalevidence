import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SearchResults } from "./SearchResults";

describe("SearchResults", () => {
  it("retains an exact canonical URI through grouping and keeps expansion separate", () => {
    const uri = "https://universalevidence.com/vocab/interventions/NutritionEducation";
    render(
      <SearchResults
        results={[
          { source: "AEA", study_id: "ONE", intervention_concept: "Nutrition education", intervention_concept_uri: uri },
          { source: "ISRCTN", study_id: "TWO", intervention_concept: "Nutrition education", intervention_concept_uri: uri },
        ]}
      />,
    );

    const link = screen.getByRole("link", { name: "Open concept page for Nutrition education" });
    expect(link).toHaveAttribute("href", uri);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    const expand = screen.getByRole("button", { name: /Nutrition education.*2 studies/i });
    expect(expand.parentElement).toBe(link.parentElement);
    expect(expand.contains(link)).toBe(false);
  });

  it("links an exact canonical State URI on condition-grouped results", () => {
    const uri = "https://universalevidence.com/vocab/states/Malaria";
    render(
      <SearchResults
        groupBy="condition"
        results={[{
          source: "ISRCTN",
          study_id: "STATE-ONE",
          condition_concept: "Malaria",
          condition_concept_uri: uri,
        }]}
      />,
    );

    expect(screen.getByRole("link", { name: "Open concept page for Malaria" })).toHaveAttribute("href", uri);
  });

  it("does not link invalid group URIs or change study and outcome-tag links", async () => {
    const { container } = render(
      <SearchResults
        results={[{
          source: "AEA",
          study_id: "ONE",
          title: "Study title",
          url: "https://example.test/study",
          intervention_concept: "Foreign concept",
          intervention_concept_uri: "https://example.com/vocab/interventions/Foreign",
          outcomes: [{ state_concept: "Anthropometry" }],
        }]}
      />,
    );

    expect(screen.queryByRole("link", { name: "Open concept page for Foreign concept" })).not.toBeInTheDocument();
    screen.getByRole("button", { name: /Foreign concept/ }).click();
    expect(await screen.findByRole("link", { name: "Study title" })).toHaveAttribute("href", "https://example.test/study");
    expect(screen.getAllByText("Anthropometry").length).toBeGreaterThan(0);
    expect(container.querySelectorAll(".outcome-tag a")).toHaveLength(0);
  });

  it("groups region-only results by condition when no axis is selected", () => {
    render(
      <SearchResults
        groupBy="condition"
        results={[{
          source: "CT.gov",
          study_id: "NCT-REGION-1",
          title: "Country evidence",
          condition_concept: "Sanitation access",
          intervention_concept: "Water treatment",
          status: "completed",
          outcomes: [],
        }]}
      />,
    );

    expect(screen.getByText("1 study · 1 condition")).toBeInTheDocument();
    expect(screen.getByText("Sanitation access")).toBeInTheDocument();
  });

  it("counts and renders unique studies in the flat fallback", () => {
    render(
      <SearchResults
        groupBy="condition"
        results={[
          {
            source: "CT.gov",
            study_id: "NCT-DUPLICATE",
            title: "One expanded study",
            intervention: "Intervention A",
            status: "completed",
            outcomes: [],
          },
          {
            source: "CT.gov",
            study_id: "NCT-DUPLICATE",
            title: "One expanded study",
            intervention: "Intervention B",
            status: "completed",
            outcomes: [],
          },
          {
            source: "ISRCTN",
            study_id: "ISRCTN-SECOND",
            title: "A second study",
            outcomes: [],
          },
        ]}
      />,
    );

    expect(screen.getByText("2 studies found")).toBeInTheDocument();
    expect(screen.getAllByText("One expanded study")).toHaveLength(1);
  });

  it("renders current backend fields and defensive outcome formats", () => {
    render(
      <SearchResults
        results={[
          {
            source: "AEA",
            study_id: "AEARCTR-0002019",
            title: "Integrated Sanitation and Nutrition Behavior Change in Kenya",
            url: "https://example.test/study",
            intervention: "Nutrition education",
            country: "KE",
            status: "completed",
            year: 2019,
            outcomes: [
              "Anthropometry",
              {
                type: "pre_specified",
                state_concept: "Anthropometry",
                measure: "Weight-for-age",
                description: "Child growth",
                time_frame: "12 months",
                summary_statistic: "Mean difference",
              },
              { unexpected: "fallback" },
            ],
          },
          {
            source: "CT.gov",
            study_id: "NCT00000000",
            title: "Trial without outcomes",
            outcomes: [],
          },
        ]}
      />,
    );

    const linkedTitle = screen.getByRole("link", {
      name: "Integrated Sanitation and Nutrition Behavior Change in Kenya",
    });
    expect(linkedTitle).toHaveAttribute("href", "https://example.test/study");
    expect(screen.getByText("AEA")).toBeInTheDocument();
    expect(screen.getByText("AEARCTR-0002019")).toBeInTheDocument();
    expect(screen.getByText("Nutrition education")).toBeInTheDocument();
    expect(screen.getByText("KE")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("2019")).toBeInTheDocument();
    expect(screen.getByText("Anthropometry")).toBeInTheDocument();
  });

  it("groups a study under every direct intervention and excludes its blank duplicate from Other", () => {
    const study = {
      source: "AEA",
      study_id: "AEARCTR-0003248",
      title: "GroMoTo",
      status: "completed",
      outcomes: [],
    };
    render(
      <SearchResults
        results={[
          { ...study, intervention_concept_uri: "urn:intervention:nutrition", intervention_concept: "Nutrition education" },
          { ...study, intervention_concept_uri: "urn:intervention:counseling", intervention_concept: "Counseling interventions" },
          { ...study, intervention_concept_uri: "urn:intervention:cash", intervention_concept: "Cash transfer" },
          { ...study, intervention_concept_uri: "urn:intervention:home", intervention_concept: "Home visiting programs" },
          { ...study, intervention_concept_uri: "urn:intervention:nutrition", intervention_concept: "Nutrition education" },
          { ...study, intervention: "unmapped duplicate row" },
        ]}
      />,
    );

    expect(screen.getByText("1 study · 4 interventions")).toBeInTheDocument();
    expect(screen.getByText("Nutrition education")).toBeInTheDocument();
    expect(screen.getByText("Counseling interventions")).toBeInTheDocument();
    expect(screen.getByText("Cash transfer")).toBeInTheDocument();
    expect(screen.getByText("Home visiting programs")).toBeInTheDocument();
    expect(screen.queryByText("Other studies")).not.toBeInTheDocument();
  });

  it("puts only wholly unmapped studies in Other studies", () => {
    render(
      <SearchResults
        results={[
          {
            source: "AEA",
            study_id: "MAPPED",
            intervention_concept_uri: "urn:intervention:cash",
            intervention_concept: "Cash transfer",
            outcomes: [],
          },
          {
            source: "AEA",
            study_id: "UNMAPPED",
            intervention: "Unclassified program",
            outcomes: [],
          },
        ]}
      />,
    );

    expect(screen.getByText("2 studies · 1 intervention")).toBeInTheDocument();
    expect(screen.getByText("Other studies")).toBeInTheDocument();
  });

  it("counts same-named study IDs from different sources separately within a group", () => {
    render(
      <SearchResults
        results={[
          {
            source: "AEA",
            study_id: "SHARED-ID",
            intervention_concept_uri: "urn:intervention:cash",
            intervention_concept: "Cash transfer",
            outcomes: [],
          },
          {
            source: "WHO ICTRP",
            study_id: "SHARED-ID",
            intervention_concept_uri: "urn:intervention:cash",
            intervention_concept: "Cash transfer",
            outcomes: [],
          },
        ]}
      />,
    );

    expect(screen.getByText("2 studies · 1 intervention")).toBeInTheDocument();
    expect(screen.getByText("2 studies", { selector: ".group-count" })).toBeInTheDocument();
  });

  it("ranks groups by total studies regardless of completion status", () => {
    const { container } = render(
      <SearchResults
        results={[
          {
            source: "AEA",
            study_id: "COMPLETED-WITH-OUTCOME",
            intervention_concept_uri: "urn:intervention:completed",
            intervention_concept: "Completed intervention",
            status: "completed",
            outcomes: [{ state_concept: "Anthropometry" }],
          },
          {
            source: "CT.gov",
            study_id: "ONGOING-1",
            intervention_concept_uri: "urn:intervention:larger",
            intervention_concept: "Larger intervention",
            status: "on_going",
            outcomes: [],
          },
          {
            source: "ISRCTN",
            study_id: "DEVELOPMENT-1",
            intervention_concept_uri: "urn:intervention:larger",
            intervention_concept: "Larger intervention",
            status: "in_development",
            outcomes: [],
          },
        ]}
      />,
    );

    const groupLabels = Array.from(container.querySelectorAll(".group-label"), (label) => label.textContent);
    expect(groupLabels).toEqual(["Larger intervention", "Completed intervention"]);
  });

  it("renders an empty result state", () => {
    render(<SearchResults results={[]} />);
    expect(screen.getByText("No studies matched the selected filters.")).toBeInTheDocument();
  });
});
