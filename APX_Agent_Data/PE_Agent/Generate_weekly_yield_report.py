import os
import pandas as pd
import glob

def _to_ratio(series: pd.Series) -> pd.Series:
	"""Convert percent-like values to 0-1 ratio."""
	s = (
		series.astype(str)
		.str.strip()
		.str.replace("%", "", regex=False)
		.str.replace(",", "", regex=False)
	)
	num = pd.to_numeric(s, errors="coerce")
	return num / 100.0


def _to_number(series: pd.Series) -> pd.Series:
	s = series.astype(str).str.strip().str.replace(",", "", regex=False)
	return pd.to_numeric(s, errors="coerce")


def _week_to_year(week_value) -> int:
	week_text = str(week_value).strip().upper()
	if week_text == "W52":
		return 2025
	return 2026


def _week_number(week_value) -> int:
	week_text = str(week_value).strip().upper()
	num = None
	if week_text.startswith("W"):
		num = pd.to_numeric(week_text[1:], errors="coerce")
	else:
		num = pd.to_numeric(week_text, errors="coerce")
	if pd.isna(num):
		return 0
	return int(num)


def build_weekly_yield_report(
	file_path: str,
	export_path: str,
	sheet_name: str = "lot Single yield analysis",
) -> pd.DataFrame:
	df = pd.read_excel(file_path, sheet_name=sheet_name)

	required_cols = ["Week", "Lot", "Die", "yield", "Particle lost"]
	missing = [c for c in required_cols if c not in df.columns]
	if missing:
		raise ValueError(f"缺少必要欄位: {missing}")

	df["Week"] = df["Week"].ffill()
	df = df[df["Week"].notna()].copy()

	df["Die_num"] = _to_number(df["Die"])
	df["yield_ratio"] = _to_ratio(df["yield"])
	df["particle_lost_ratio"] = _to_ratio(df["Particle lost"])
	df["particle_yield_ratio"] = 1 - df["particle_lost_ratio"]

	if "Particle" in df.columns:
		df["Particle_num"] = _to_number(df["Particle"])
	else:
		df["Particle_num"] = pd.NA

	if df["Particle_num"].notna().any():
		df["particle_loss_count"] = df["Particle_num"]
	else:
		# Fallback: use Die * Particle lost ratio when Particle count is unavailable.
		df["particle_loss_count"] = (df["Die_num"] * df["particle_lost_ratio"]).fillna(0)

	weekly = (
		df.groupby("Week", dropna=False)
		.agg(
			total_die=("Die_num", "sum"),
			total_lots=("Lot", pd.Series.nunique),
			avg_yield_ratio=("yield_ratio", "mean"),
			avg_particle_yield_ratio=("particle_yield_ratio", "mean"),
			total_particle_loss=("particle_loss_count", "sum"),
		)
		.reset_index()
	)

	weekly["年碼"] = weekly["Week"].apply(_week_to_year)
	weekly["_week_num"] = weekly["Week"].apply(_week_number)
	weekly = weekly.sort_values(["年碼", "_week_num"], ascending=[True, True])

	weekly["平均總良率"] = (weekly["avg_yield_ratio"] * 100).round(2).astype(str) + "%"
	weekly["平均Particle Yield"] = (
		(weekly["avg_particle_yield_ratio"] * 100).round(2).astype(str) + "%"
	)

	export_df = weekly[
		[
			"年碼",
			"Week",
			"total_die",
			"total_lots",
			"平均總良率",
			"平均Particle Yield",
			"total_particle_loss",
		]
	].rename(
		columns={
			"Week": "週次",
			"total_die": "總產量(Die)",
			"total_lots": "總Lot數",
			"total_particle_loss": "Particle損失總棵數",
		}
	)

	export_df["總產量(Die)"] = export_df["總產量(Die)"].fillna(0).round(0).astype(int)
	export_df["總Lot數"] = export_df["總Lot數"].fillna(0).astype(int)
	export_df["Particle損失總棵數"] = (
		export_df["Particle損失總棵數"].fillna(0).round(0).astype(int)
	)

	os.makedirs(os.path.dirname(export_path), exist_ok=True)
	export_df.to_excel(export_path, index=False)
	return export_df

def get_latest_xlsx(folder: str) -> str:
    files = glob.glob(os.path.join(folder, "*.xlsx"))
    if not files:
        raise FileNotFoundError(f"找不到任何 xlsx 檔案：{folder}")
    return max(files, key=os.path.getmtime)
	
if __name__ == "__main__":
	# ============================================================
	file_path = get_latest_xlsx(r"\\khaiotapvp02\AMD\Yield")
	# file_path = r"\\khaiotapvp02\AMD\Yield\AMD Weisshorn lot RDL Yield_0527_(Security C).xlsx"
	local_export_dir = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
	local_structured_excel_path = os.path.join(
		local_export_dir, "weekly_yield_report_(Security C).xlsx"	
	)

	result_df = build_weekly_yield_report(
		file_path=file_path,
		export_path=local_structured_excel_path,
		sheet_name="lot Single yield analysis",
	)
	print("weekly report generated:", local_structured_excel_path)
	# print(result_df)
