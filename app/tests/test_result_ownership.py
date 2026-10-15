import asyncio
from unittest.mock import AsyncMock

import pytest

from app.adapters.web.schemas import AccessoryInput, ValidationReportResponse
from app.main import app, lifespan
from app.adapters.persistence.postgresql import PostgresValidationReportRepository
from app.domain.criterion import CriterionAnswer, CriterionResult
from app.domain.errors import (
    ValidationConfigurationError,
    ValidationExecutionError,
)
from app.domain.product import (
    Product,
    ProductContext,
    ProductOrigin,
    ProductType,
)
from app.domain.validation import (
    Validation,
    ValidationRequest,
)
from app.domain.validation_results import (
    ReportStatus,
    ValidationExecution,
    ValidationReport,
    ValidationResult,
    ValidationStatus,
)
from app.domain.validation_service import (
    ProductValidationService,
)


def product(*, external_ref: str = "ACC-42", category: str | None = None) -> Product:
    return Product(
        product_type=ProductType.ACCESSORY,
        brand="Example",
        model="Rack",
        category=category,
        origin=ProductOrigin(source="odoo", external_ref=external_ref),
    )


def validation_result():
    return ValidationResult(
        status=ValidationStatus.PASSED,
        details="Accessory is allowed.",
    )


def test_every_input_gets_a_new_product_id_even_with_the_same_origin():
    first = product()
    second = product()

    assert first.id != second.id
    assert first.origin == second.origin


def test_accessory_input_maps_origin_and_creates_a_new_product():
    submitted = AccessoryInput.model_validate(
        {
            "brand": "Example",
            "model": "Rack",
            "year": 2026,
            "category": "transport",
            "price": "49.99",
            "context": {},
            "origin": {"source": "odoo", "external_ref": "ACC-42"},
        }
    )

    first = submitted.to_domain().product
    second = submitted.to_domain().product

    assert first.id != second.id
    assert first.origin == ProductOrigin(source="odoo", external_ref="ACC-42")
    assert first.year == 2026
    assert first.category == "transport"


def test_validation_execution_owns_its_result():
    associated_product = product()

    execution = ValidationExecution(
        validation_id="accessory_leasability",
        product=associated_product,
        result=validation_result(),
    )

    assert execution.product is associated_product
    assert execution.result.status is ValidationStatus.PASSED
    assert not hasattr(execution, "product_id")


def test_validation_creates_an_execution():
    associated_product = product()

    class AlwaysPasses(Validation):
        id = "always_passes"

        async def evaluate_result(self, request):
            return validation_result()

    execution = asyncio.run(
        AlwaysPasses().validate(ValidationRequest(product=associated_product))
    )

    assert execution.product is associated_product
    assert execution.validation_id == "always_passes"
    assert execution.result.status is ValidationStatus.PASSED


def test_service_rejects_a_validation_execution_for_another_product():
    requested_product = product()
    other_product = product(external_ref="OTHER")

    class WrongProductValidation:
        id = "accessory_leasability"

        async def validate(self, request):
            return ValidationExecution(
                product=other_product,
                validation_id=self.id,
                result=validation_result(),
            )

    service = ProductValidationService([WrongProductValidation()], AsyncMock())

    with pytest.raises(ValidationExecutionError, match="failed"):
        asyncio.run(service.validate(ValidationRequest(product=requested_product)))


def test_service_saves_complete_report_before_returning():
    repository = AsyncMock()
    criterion = CriterionResult(
        CriterionAnswer.NO, "Override.", "special_rules"
    )

    class Check(Validation):
        id = "check"

        async def evaluate_result(self, request):
            return ValidationResult(
                status=ValidationStatus.REJECTED,
                details="Not leasable.",
                criterion_results=(criterion,),
            )

    class OtherCheck(Validation):
        id = "other_check"

        async def evaluate_result(self, request):
            return validation_result()

    submitted = product()
    report = asyncio.run(
        ProductValidationService([Check(), OtherCheck()], repository).validate(
            ValidationRequest(product=submitted)
        )
    )

    repository.save.assert_awaited_once_with(report)
    assert report.product is submitted
    assert report.status is ReportStatus.INVALID
    assert [execution.validation_id for execution in report.validations] == [
        "check", "other_check"
    ]
    assert report.validations[0].result.criterion_results == (criterion,)


def test_service_does_not_save_incomplete_report():
    repository = AsyncMock()

    class FailingCheck(Validation):
        id = "failing_check"

        async def evaluate_result(self, request):
            raise RuntimeError("No response")

    with pytest.raises(ValidationExecutionError):
        asyncio.run(
            ProductValidationService([FailingCheck()], repository).validate(
                ValidationRequest(product=product())
            )
        )
    repository.save.assert_not_awaited()


def test_service_rejects_missing_repository():
    with pytest.raises(ValidationConfigurationError, match="repository is required"):
        ProductValidationService([], None)


def test_app_startup_configures_postgres_repository(auth_context, monkeypatch):
    monkeypatch.setattr("app.main.settings", auth_context.settings)
    async def start():
        async with lifespan(app):
            assert isinstance(
                app.state.validation_report_repository, PostgresValidationReportRepository
            )

    asyncio.run(start())


def test_service_does_not_return_report_when_save_fails():
    repository = AsyncMock()
    repository.save.side_effect = RuntimeError("Database unavailable")

    class Check(Validation):
        id = "check"

        async def evaluate_result(self, request):
            return validation_result()

    with pytest.raises(RuntimeError, match="Database unavailable"):
        asyncio.run(
            ProductValidationService([Check()], repository).validate(
                ValidationRequest(product=product())
            )
        )
    repository.save.assert_awaited_once()


def test_validation_result_criterion_results_default_to_empty():
    assert validation_result().criterion_results == ()


def test_report_response_exposes_criterion_results():
    associated_product = product()
    result = ValidationResult(
        status=ValidationStatus.PASSED,
        details="Rack is leasable.",
        criterion_results=[
            CriterionResult(CriterionAnswer.YES, "Fixed.", criterion_id="rack"),
            CriterionResult(
                CriterionAnswer.NO,
                "Override.",
                criterion_id="special_rules",
            ),
        ],
    )
    report = ValidationReport(
        product=associated_product,
        status=ReportStatus.VALID,
        validations=(
            ValidationExecution(
                product=associated_product, validation_id="leasability", result=result
            ),
        ),
    )

    response = ValidationReportResponse.from_domain(report).model_dump(mode="json")

    assert response["validations"][0]["criterion_results"] == [
        {"criterion_id": "rack", "answer": "YES", "details": "Fixed."},
        {
            "criterion_id": "special_rules",
            "answer": "NO",
            "details": "Override.",
        },
    ]


def test_api_preserves_product_context_and_defaults_to_non_bawu():
    payload = {
        "brand": "Example",
        "model": "Rack",
        "price": "49.99",
        "origin": {"source": "odoo", "external_ref": "ACC-42"},
        "context": {"is_bawu_order": True},
    }
    assert AccessoryInput.model_validate(payload).to_domain().context == ProductContext(
        is_bawu_order=True
    )
    payload["context"] = {}
    assert (
        AccessoryInput.model_validate(payload).to_domain().context == ProductContext()
    )
    assert ValidationRequest(product=product()).context.is_bawu_order is False
