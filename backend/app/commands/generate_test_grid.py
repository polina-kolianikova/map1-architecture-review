from app.db.session import SessionLocal
from app.services.grid import generate_test_grid


def main() -> None:
    with SessionLocal() as db:
        result = generate_test_grid(db)
        db.commit()
    print(
        "grid created "
        f"region_code={result['region_code']} "
        f"grid_version={result['grid_version']} "
        f"cell_count={result['cell_count']}"
    )


if __name__ == "__main__":
    main()
