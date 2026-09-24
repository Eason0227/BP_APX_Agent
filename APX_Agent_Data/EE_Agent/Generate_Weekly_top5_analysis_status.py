import glob
import os
import re
from typing import List, Optional, Tuple

import pandas as pd


def parse_week_from_filename(path: str) -> Optional[str]:
	"""Extract week token from file name, e.g. top5_machines_2026-W17.csv."""
	name = os.path.basename(path)
	match = re.search(r"top5_machines_(\d{4}-W\d{2})\.csv$", name)
	return match.group(1) if match else None	


def week_sort_key(week: str) -> Tuple[int, int]:
	year_str, week_str = week.split("-W")
	return int(year_str), int(week_str)


def load_top5_data(data_dir: str) -> Tuple[pd.DataFrame, List[str]]:
	pattern = os.path.join(data_dir, "top5_machines_*.csv")
	files = glob.glob(pattern)
	if not files:
		raise FileNotFoundError(f"No files found by pattern: {pattern}")

	frames: List[pd.DataFrame] = []
	for file_path in files:
		week = parse_week_from_filename(file_path)
		if not week:
			continue

		df = pd.read_csv(file_path)
		if "Machine_ID" not in df.columns:
			raise ValueError(f"Column Machine_ID missing in: {file_path}")

		if "Weighted_Risk_index" not in df.columns:
			df["Weighted_Risk_index"] = 1.0

		df["Week"] = week
		frames.append(df)

	if not frames:
		raise ValueError("No valid top5 data loaded. Check file names and columns.")

	top5 = pd.concat(frames, ignore_index=True)
	top5["Machine_ID"] = top5["Machine_ID"].astype(str)
	top5["Week"] = top5["Week"].astype(str)
	top5["Weighted_Risk_index"] = pd.to_numeric(
		top5["Weighted_Risk_index"], errors="coerce"
	).fillna(0.0)

	weeks = sorted(top5["Week"].unique(), key=week_sort_key)
	return top5, weeks


def merged_data_by_week(top5: pd.DataFrame, weeks: List[str]) -> pd.DataFrame:
	merged = top5.copy()
	week_order = {w: i for i, w in enumerate(weeks)}
	merged["_order"] = merged["Week"].map(week_order)
	merged = merged.sort_values(["_order", "Machine_ID"]).drop(columns=["_order"])
	return merged.reset_index(drop=True)


def export_to_excel(
	output_path: str,
	df_merged_weekly: pd.DataFrame,
) -> None:
	df_to_write = df_merged_weekly.copy()

	if os.path.exists(output_path):
		try:
			df_existing = pd.read_excel(output_path, sheet_name="Merged_By_Week")
			df_to_write = pd.concat([df_existing, df_merged_weekly], ignore_index=True)
			df_to_write = df_to_write.drop_duplicates().reset_index(drop=True)
		except Exception:
			# If existing file cannot be read, fallback to writing current run data only.
			df_to_write = df_merged_weekly.copy()

	with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
		df_to_write.to_excel(writer, sheet_name="Merged_By_Week", index=False)


def main() -> None:

	data_dir = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\particle_yield_analysis_machine_report\top5_machine_stats"
	top5, weeks = load_top5_data(data_dir)
	df_merged_weekly = merged_data_by_week(top5, weeks)
	script_dir = os.path.dirname(os.path.abspath(__file__))
	output_excel = os.path.join(script_dir, "weekly_report_top5_machine_(Security C).xlsx")
	export_to_excel(output_excel, df_merged_weekly)

	print(f"Weeks loaded: {', '.join(weeks)}")
	# print("\nMerged data by week (all rows/columns):")
	# print(df_merged_weekly.to_string(index=False))
	print(f"\nExcel saved: {output_excel}")


if __name__ == "__main__":
	main()
