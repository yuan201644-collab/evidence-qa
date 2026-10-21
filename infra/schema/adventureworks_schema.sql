--
-- PostgreSQL database dump
--

-- Dumped from database version 16.9 (Debian 16.9-1.pgdg120+1)
-- Dumped by pg_dump version 16.9 (Debian 16.9-1.pgdg120+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: HumanResources; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "HumanResources";


--
-- Name: Person; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "Person";


--
-- Name: Production; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "Production";


--
-- Name: Purchasing; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "Purchasing";


--
-- Name: Sales; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA "Sales";


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: st_geomfromtext(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.st_geomfromtext(input text) RETURNS text
    LANGUAGE sql
    RETURN input;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: Department; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."Department" (
    "DepartmentID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "GroupName" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Department_DepartmentID_seq; Type: SEQUENCE; Schema: HumanResources; Owner: -
--

CREATE SEQUENCE "HumanResources"."Department_DepartmentID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Department_DepartmentID_seq; Type: SEQUENCE OWNED BY; Schema: HumanResources; Owner: -
--

ALTER SEQUENCE "HumanResources"."Department_DepartmentID_seq" OWNED BY "HumanResources"."Department"."DepartmentID";


--
-- Name: Employee; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."Employee" (
    "BusinessEntityID" integer NOT NULL,
    "NationalIDNumber" character varying(15) NOT NULL,
    "LoginID" character varying(256) NOT NULL,
    "OrganizationNode" text,
    "OrganizationLevel" integer,
    "JobTitle" character varying(50) NOT NULL,
    "BirthDate" date NOT NULL,
    "MaritalStatus" character(1) NOT NULL,
    "Gender" character(1) NOT NULL,
    "HireDate" date NOT NULL,
    "SalariedFlag" boolean DEFAULT true NOT NULL,
    "VacationHours" smallint DEFAULT 0 NOT NULL,
    "SickLeaveHours" smallint DEFAULT 0 NOT NULL,
    "CurrentFlag" boolean DEFAULT true NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Employee_BirthDate" CHECK ((("BirthDate" >= '1930-01-01'::date) AND ("BirthDate" <= (CURRENT_DATE + '-18 years'::interval)))),
    CONSTRAINT "CK_Employee_Gender" CHECK (((upper(("Gender")::text) = 'F'::text) OR (upper(("Gender")::text) = 'M'::text))),
    CONSTRAINT "CK_Employee_HireDate" CHECK ((("HireDate" >= '1996-07-01'::date) AND ("HireDate" <= (CURRENT_DATE + '1 day'::interval)))),
    CONSTRAINT "CK_Employee_MaritalStatus" CHECK (((upper(("MaritalStatus")::text) = 'S'::text) OR (upper(("MaritalStatus")::text) = 'M'::text))),
    CONSTRAINT "CK_Employee_SickLeaveHours" CHECK ((("SickLeaveHours" >= 0) AND ("SickLeaveHours" <= 120))),
    CONSTRAINT "CK_Employee_VacationHours" CHECK ((("VacationHours" >= '-40'::integer) AND ("VacationHours" <= 240)))
);


--
-- Name: EmployeeDepartmentHistory; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."EmployeeDepartmentHistory" (
    "BusinessEntityID" integer NOT NULL,
    "DepartmentID" smallint NOT NULL,
    "ShiftID" smallint NOT NULL,
    "StartDate" date NOT NULL,
    "EndDate" date,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_EmployeeDepartmentHistory_EndDate" CHECK ((("EndDate" >= "StartDate") OR ("EndDate" IS NULL)))
);


--
-- Name: EmployeePayHistory; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."EmployeePayHistory" (
    "BusinessEntityID" integer NOT NULL,
    "RateChangeDate" timestamp without time zone NOT NULL,
    "Rate" numeric(19,4) NOT NULL,
    "PayFrequency" smallint NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_EmployeePayHistory_PayFrequency" CHECK ((("PayFrequency" = 2) OR ("PayFrequency" = 1))),
    CONSTRAINT "CK_EmployeePayHistory_Rate" CHECK ((("Rate" >= 6.50) AND ("Rate" <= 200.00)))
);


--
-- Name: JobCandidate; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."JobCandidate" (
    "JobCandidateID" integer NOT NULL,
    "BusinessEntityID" integer,
    "Resume" xml,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: JobCandidate_JobCandidateID_seq; Type: SEQUENCE; Schema: HumanResources; Owner: -
--

CREATE SEQUENCE "HumanResources"."JobCandidate_JobCandidateID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: JobCandidate_JobCandidateID_seq; Type: SEQUENCE OWNED BY; Schema: HumanResources; Owner: -
--

ALTER SEQUENCE "HumanResources"."JobCandidate_JobCandidateID_seq" OWNED BY "HumanResources"."JobCandidate"."JobCandidateID";


--
-- Name: Shift; Type: TABLE; Schema: HumanResources; Owner: -
--

CREATE TABLE "HumanResources"."Shift" (
    "ShiftID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "StartTime" time(6) without time zone NOT NULL,
    "EndTime" time(6) without time zone NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Shift_ShiftID_seq; Type: SEQUENCE; Schema: HumanResources; Owner: -
--

CREATE SEQUENCE "HumanResources"."Shift_ShiftID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Shift_ShiftID_seq; Type: SEQUENCE OWNED BY; Schema: HumanResources; Owner: -
--

ALTER SEQUENCE "HumanResources"."Shift_ShiftID_seq" OWNED BY "HumanResources"."Shift"."ShiftID";


--
-- Name: Address; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."Address" (
    "AddressID" integer NOT NULL,
    "AddressLine1" character varying(60) NOT NULL,
    "AddressLine2" character varying(60),
    "City" character varying(30) NOT NULL,
    "StateProvinceID" integer NOT NULL,
    "PostalCode" character varying(15) NOT NULL,
    "SpatialLocation" text,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: BusinessEntityAddress; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."BusinessEntityAddress" (
    "BusinessEntityID" integer NOT NULL,
    "AddressID" integer NOT NULL,
    "AddressTypeID" integer NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: CountryRegion; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."CountryRegion" (
    "CountryRegionCode" character varying(3) NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: EmailAddress; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."EmailAddress" (
    "BusinessEntityID" integer NOT NULL,
    "EmailAddressID" integer NOT NULL,
    "EmailAddress" character varying(50),
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Person; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."Person" (
    "BusinessEntityID" integer NOT NULL,
    "PersonType" character(2) NOT NULL,
    "NameStyle" character varying(256) DEFAULT 0 NOT NULL,
    "Title" character varying(8),
    "FirstName" character varying(256) NOT NULL,
    "MiddleName" character varying(256),
    "LastName" character varying(256) NOT NULL,
    "Suffix" character varying(10),
    "EmailPromotion" integer DEFAULT 0 NOT NULL,
    "AdditionalContactInfo" xml,
    "Demographics" xml,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Person_EmailPromotion" CHECK ((("EmailPromotion" >= 0) AND ("EmailPromotion" <= 2))),
    CONSTRAINT "CK_Person_PersonType" CHECK ((("PersonType" IS NULL) OR ((upper(("PersonType")::text) = 'GC'::text) OR (upper(("PersonType")::text) = 'SP'::text) OR (upper(("PersonType")::text) = 'EM'::text) OR (upper(("PersonType")::text) = 'IN'::text) OR (upper(("PersonType")::text) = 'VC'::text) OR (upper(("PersonType")::text) = 'SC'::text))))
);


--
-- Name: PersonPhone; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."PersonPhone" (
    "BusinessEntityID" integer NOT NULL,
    "PhoneNumber" character varying(64) NOT NULL,
    "PhoneNumberTypeID" integer NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: PhoneNumberType; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."PhoneNumberType" (
    "PhoneNumberTypeID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: StateProvince; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."StateProvince" (
    "StateProvinceID" integer NOT NULL,
    "StateProvinceCode" character(3) NOT NULL,
    "CountryRegionCode" character varying(3) NOT NULL,
    "IsOnlyStateProvinceFlag" boolean DEFAULT true NOT NULL,
    "Name" character varying(256) NOT NULL,
    "TerritoryID" integer NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: vEmployee; Type: VIEW; Schema: HumanResources; Owner: -
--

CREATE VIEW "HumanResources"."vEmployee" AS
 SELECT e."BusinessEntityID",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    e."JobTitle",
    pp."PhoneNumber",
    pnt."Name" AS "PhoneNumberType",
    ea."EmailAddress",
    p."EmailPromotion",
    a."AddressLine1",
    a."AddressLine2",
    a."City",
    sp."Name" AS "StateProvinceName",
    a."PostalCode",
    cr."Name" AS "CountryRegionName",
    p."AdditionalContactInfo"
   FROM (((((((("HumanResources"."Employee" e
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = e."BusinessEntityID")))
     JOIN "Person"."BusinessEntityAddress" bea ON ((bea."BusinessEntityID" = e."BusinessEntityID")))
     JOIN "Person"."Address" a ON ((a."AddressID" = bea."AddressID")))
     JOIN "Person"."StateProvince" sp ON ((sp."StateProvinceID" = a."StateProvinceID")))
     JOIN "Person"."CountryRegion" cr ON (((cr."CountryRegionCode")::text = (sp."CountryRegionCode")::text)))
     LEFT JOIN "Person"."PersonPhone" pp ON ((pp."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PhoneNumberType" pnt ON ((pp."PhoneNumberTypeID" = pnt."PhoneNumberTypeID")))
     LEFT JOIN "Person"."EmailAddress" ea ON ((p."BusinessEntityID" = ea."BusinessEntityID")));


--
-- Name: vEmployeeDepartment; Type: VIEW; Schema: HumanResources; Owner: -
--

CREATE VIEW "HumanResources"."vEmployeeDepartment" AS
 SELECT e."BusinessEntityID",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    e."JobTitle",
    d."Name" AS "Department",
    d."GroupName",
    edh."StartDate"
   FROM ((("HumanResources"."Employee" e
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = e."BusinessEntityID")))
     JOIN "HumanResources"."EmployeeDepartmentHistory" edh ON ((e."BusinessEntityID" = edh."BusinessEntityID")))
     JOIN "HumanResources"."Department" d ON ((edh."DepartmentID" = d."DepartmentID")))
  WHERE (edh."EndDate" IS NULL);


--
-- Name: vEmployeeDepartmentHistory; Type: VIEW; Schema: HumanResources; Owner: -
--

CREATE VIEW "HumanResources"."vEmployeeDepartmentHistory" AS
 SELECT e."BusinessEntityID",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    s."Name" AS "Shift",
    d."Name" AS "Department",
    d."GroupName",
    edh."StartDate",
    edh."EndDate"
   FROM (((("HumanResources"."Employee" e
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = e."BusinessEntityID")))
     JOIN "HumanResources"."EmployeeDepartmentHistory" edh ON ((e."BusinessEntityID" = edh."BusinessEntityID")))
     JOIN "HumanResources"."Department" d ON ((edh."DepartmentID" = d."DepartmentID")))
     JOIN "HumanResources"."Shift" s ON ((s."ShiftID" = edh."ShiftID")));


--
-- Name: AddressType; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."AddressType" (
    "AddressTypeID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: AddressType_AddressTypeID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."AddressType_AddressTypeID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: AddressType_AddressTypeID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."AddressType_AddressTypeID_seq" OWNED BY "Person"."AddressType"."AddressTypeID";


--
-- Name: Address_AddressID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."Address_AddressID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Address_AddressID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."Address_AddressID_seq" OWNED BY "Person"."Address"."AddressID";


--
-- Name: BusinessEntity; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."BusinessEntity" (
    "BusinessEntityID" integer NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: BusinessEntityContact; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."BusinessEntityContact" (
    "BusinessEntityID" integer NOT NULL,
    "PersonID" integer NOT NULL,
    "ContactTypeID" integer NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: BusinessEntity_BusinessEntityID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."BusinessEntity_BusinessEntityID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: BusinessEntity_BusinessEntityID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."BusinessEntity_BusinessEntityID_seq" OWNED BY "Person"."BusinessEntity"."BusinessEntityID";


--
-- Name: ContactType; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."ContactType" (
    "ContactTypeID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ContactType_ContactTypeID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."ContactType_ContactTypeID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ContactType_ContactTypeID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."ContactType_ContactTypeID_seq" OWNED BY "Person"."ContactType"."ContactTypeID";


--
-- Name: EmailAddress_EmailAddressID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."EmailAddress_EmailAddressID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: EmailAddress_EmailAddressID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."EmailAddress_EmailAddressID_seq" OWNED BY "Person"."EmailAddress"."EmailAddressID";


--
-- Name: Password; Type: TABLE; Schema: Person; Owner: -
--

CREATE TABLE "Person"."Password" (
    "BusinessEntityID" integer NOT NULL,
    "PasswordHash" character varying(128) NOT NULL,
    "PasswordSalt" character varying(10) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: PhoneNumberType_PhoneNumberTypeID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."PhoneNumberType_PhoneNumberTypeID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: PhoneNumberType_PhoneNumberTypeID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."PhoneNumberType_PhoneNumberTypeID_seq" OWNED BY "Person"."PhoneNumberType"."PhoneNumberTypeID";


--
-- Name: StateProvince_StateProvinceID_seq; Type: SEQUENCE; Schema: Person; Owner: -
--

CREATE SEQUENCE "Person"."StateProvince_StateProvinceID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: StateProvince_StateProvinceID_seq; Type: SEQUENCE OWNED BY; Schema: Person; Owner: -
--

ALTER SEQUENCE "Person"."StateProvince_StateProvinceID_seq" OWNED BY "Person"."StateProvince"."StateProvinceID";


--
-- Name: vStateProvinceCountryRegion; Type: MATERIALIZED VIEW; Schema: Person; Owner: -
--

CREATE MATERIALIZED VIEW "Person"."vStateProvinceCountryRegion" AS
 SELECT sp."StateProvinceID",
    sp."StateProvinceCode",
    sp."IsOnlyStateProvinceFlag",
    sp."Name" AS "StateProvinceName",
    sp."TerritoryID",
    cr."CountryRegionCode",
    cr."Name" AS "CountryRegionName"
   FROM ("Person"."StateProvince" sp
     JOIN "Person"."CountryRegion" cr ON (((sp."CountryRegionCode")::text = (cr."CountryRegionCode")::text)))
  WITH NO DATA;


--
-- Name: BillOfMaterials; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."BillOfMaterials" (
    "BillOfMaterialsID" integer NOT NULL,
    "ProductAssemblyID" integer,
    "ComponentID" integer NOT NULL,
    "StartDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "EndDate" timestamp without time zone,
    "UnitMeasureCode" character(3) NOT NULL,
    "BOMLevel" smallint NOT NULL,
    "PerAssemblyQty" numeric(8,2) DEFAULT 1.00 NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_BillOfMaterials_BOMLevel" CHECK (((("ProductAssemblyID" IS NULL) AND ("BOMLevel" = 0) AND ("PerAssemblyQty" = 1.00)) OR (("ProductAssemblyID" IS NOT NULL) AND ("BOMLevel" >= 1)))),
    CONSTRAINT "CK_BillOfMaterials_EndDate" CHECK ((("EndDate" > "StartDate") OR ("EndDate" IS NULL))),
    CONSTRAINT "CK_BillOfMaterials_PerAssemblyQty" CHECK (("PerAssemblyQty" >= 1.00)),
    CONSTRAINT "CK_BillOfMaterials_ProductAssemblyID" CHECK (("ProductAssemblyID" <> "ComponentID"))
);


--
-- Name: BillOfMaterials_BillOfMaterialsID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."BillOfMaterials_BillOfMaterialsID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: BillOfMaterials_BillOfMaterialsID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."BillOfMaterials_BillOfMaterialsID_seq" OWNED BY "Production"."BillOfMaterials"."BillOfMaterialsID";


--
-- Name: Culture; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."Culture" (
    "CultureID" character(6) NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Document; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."Document" (
    "DocumentNode" character varying(128) NOT NULL,
    "DocumentLevel" integer,
    "Title" character varying(50) NOT NULL,
    "Owner" integer NOT NULL,
    "FolderFlag" boolean DEFAULT false NOT NULL,
    "FileName" character varying(400) NOT NULL,
    "FileExtension" character varying(8) NOT NULL,
    "Revision" character(5) NOT NULL,
    "ChangeNumber" integer DEFAULT 0 NOT NULL,
    "Status" smallint NOT NULL,
    "DocumentSummary" text,
    "Document" bytea,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Document_Status" CHECK ((("Status" >= 1) AND ("Status" <= 3)))
);


--
-- Name: Illustration; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."Illustration" (
    "IllustrationID" integer NOT NULL,
    "Diagram" xml,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Illustration_IllustrationID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."Illustration_IllustrationID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Illustration_IllustrationID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."Illustration_IllustrationID_seq" OWNED BY "Production"."Illustration"."IllustrationID";


--
-- Name: Location; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."Location" (
    "LocationID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "CostRate" numeric(8,2) DEFAULT 0.00 NOT NULL,
    "Availability" numeric(8,2) DEFAULT 0.00 NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Location_Availability" CHECK (("Availability" >= 0.00)),
    CONSTRAINT "CK_Location_CostRate" CHECK (("CostRate" >= 0.00))
);


--
-- Name: Location_LocationID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."Location_LocationID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Location_LocationID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."Location_LocationID_seq" OWNED BY "Production"."Location"."LocationID";


--
-- Name: Product; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."Product" (
    "ProductID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ProductNumber" character varying(25) NOT NULL,
    "MakeFlag" smallint DEFAULT 1 NOT NULL,
    "FinishedGoodsFlag" smallint DEFAULT 1 NOT NULL,
    "Color" character varying(15),
    "SafetyStockLevel" smallint NOT NULL,
    "ReorderPoint" smallint NOT NULL,
    "StandardCost" numeric(19,4) NOT NULL,
    "ListPrice" numeric(19,4) NOT NULL,
    "Size" character varying(5),
    "SizeUnitMeasureCode" character(3),
    "WeightUnitMeasureCode" character(3),
    "Weight" numeric(8,2),
    "DaysToManufacture" integer NOT NULL,
    "ProductLine" character(2),
    "Class" character(2),
    "Style" character(2),
    "ProductSubcategoryID" integer,
    "ProductModelID" integer,
    "SellStartDate" timestamp without time zone NOT NULL,
    "SellEndDate" timestamp without time zone,
    "DiscontinuedDate" timestamp without time zone,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Product_Class" CHECK (((upper(("Class")::text) = 'H'::text) OR (upper(("Class")::text) = 'M'::text) OR (upper(("Class")::text) = 'L'::text) OR ("Class" IS NULL))),
    CONSTRAINT "CK_Product_DaysToManufacture" CHECK (("DaysToManufacture" >= 0)),
    CONSTRAINT "CK_Product_ListPrice" CHECK (("ListPrice" >= 0.00)),
    CONSTRAINT "CK_Product_ProductLine" CHECK (((upper(("ProductLine")::text) = 'R'::text) OR (upper(("ProductLine")::text) = 'M'::text) OR (upper(("ProductLine")::text) = 'T'::text) OR (upper(("ProductLine")::text) = 'S'::text) OR ("ProductLine" IS NULL))),
    CONSTRAINT "CK_Product_ReorderPoint" CHECK (("ReorderPoint" > 0)),
    CONSTRAINT "CK_Product_SafetyStockLevel" CHECK (("SafetyStockLevel" > 0)),
    CONSTRAINT "CK_Product_SellEndDate" CHECK ((("SellEndDate" >= "SellStartDate") OR ("SellEndDate" IS NULL))),
    CONSTRAINT "CK_Product_StandardCost" CHECK (("StandardCost" >= 0.00)),
    CONSTRAINT "CK_Product_Style" CHECK (((upper(("Style")::text) = 'U'::text) OR (upper(("Style")::text) = 'M'::text) OR (upper(("Style")::text) = 'W'::text) OR ("Style" IS NULL))),
    CONSTRAINT "CK_Product_Weight" CHECK (("Weight" > 0.00))
);


--
-- Name: ProductCategory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductCategory" (
    "ProductCategoryID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductCategory_ProductCategoryID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductCategory_ProductCategoryID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductCategory_ProductCategoryID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductCategory_ProductCategoryID_seq" OWNED BY "Production"."ProductCategory"."ProductCategoryID";


--
-- Name: ProductCostHistory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductCostHistory" (
    "ProductID" integer NOT NULL,
    "StartDate" timestamp without time zone NOT NULL,
    "EndDate" timestamp without time zone,
    "StandardCost" numeric(19,4) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ProductCostHistory_EndDate" CHECK ((("EndDate" >= "StartDate") OR ("EndDate" IS NULL))),
    CONSTRAINT "CK_ProductCostHistory_StandardCost" CHECK (("StandardCost" >= 0.00))
);


--
-- Name: ProductDescription; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductDescription" (
    "ProductDescriptionID" integer NOT NULL,
    "Description" character varying(400) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductDescription_ProductDescriptionID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductDescription_ProductDescriptionID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductDescription_ProductDescriptionID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductDescription_ProductDescriptionID_seq" OWNED BY "Production"."ProductDescription"."ProductDescriptionID";


--
-- Name: ProductDocument; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductDocument" (
    "ProductID" integer NOT NULL,
    "DocumentNode" character varying(128) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductInventory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductInventory" (
    "ProductID" integer NOT NULL,
    "LocationID" smallint NOT NULL,
    "Shelf" character varying(10) NOT NULL,
    "Bin" smallint NOT NULL,
    "Quantity" smallint DEFAULT 0 NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ProductInventory_Bin" CHECK ((("Bin" >= 0) AND ("Bin" <= 100)))
);


--
-- Name: ProductListPriceHistory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductListPriceHistory" (
    "ProductID" integer NOT NULL,
    "StartDate" timestamp without time zone NOT NULL,
    "EndDate" timestamp without time zone,
    "ListPrice" numeric(19,4) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ProductListPriceHistory_EndDate" CHECK ((("EndDate" >= "StartDate") OR ("EndDate" IS NULL))),
    CONSTRAINT "CK_ProductListPriceHistory_ListPrice" CHECK (("ListPrice" > 0.00))
);


--
-- Name: ProductModel; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductModel" (
    "ProductModelID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "CatalogDescription" xml,
    "Instructions" xml,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductModelIllustration; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductModelIllustration" (
    "ProductModelID" integer NOT NULL,
    "IllustrationID" integer NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductModelProductDescriptionCulture; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductModelProductDescriptionCulture" (
    "ProductModelID" integer NOT NULL,
    "ProductDescriptionID" integer NOT NULL,
    "CultureID" character(6) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductModel_ProductModelID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductModel_ProductModelID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductModel_ProductModelID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductModel_ProductModelID_seq" OWNED BY "Production"."ProductModel"."ProductModelID";


--
-- Name: ProductPhoto; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductPhoto" (
    "ProductPhotoID" integer NOT NULL,
    "ThumbNailPhoto" bytea,
    "ThumbnailPhotoFileName" character varying(50),
    "LargePhoto" bytea,
    "LargePhotoFileName" character varying(50),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductPhoto_ProductPhotoID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductPhoto_ProductPhotoID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductPhoto_ProductPhotoID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductPhoto_ProductPhotoID_seq" OWNED BY "Production"."ProductPhoto"."ProductPhotoID";


--
-- Name: ProductProductPhoto; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductProductPhoto" (
    "ProductID" integer NOT NULL,
    "ProductPhotoID" integer NOT NULL,
    "Primary" boolean DEFAULT false NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductReview; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductReview" (
    "ProductReviewID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    "ReviewerName" character varying(256) NOT NULL,
    "ReviewDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "EmailAddress" character varying(50) NOT NULL,
    "Rating" integer NOT NULL,
    "Comments" character varying(3850),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ProductReview_Rating" CHECK ((("Rating" >= 1) AND ("Rating" <= 5)))
);


--
-- Name: ProductReview_ProductReviewID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductReview_ProductReviewID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductReview_ProductReviewID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductReview_ProductReviewID_seq" OWNED BY "Production"."ProductReview"."ProductReviewID";


--
-- Name: ProductSubcategory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ProductSubcategory" (
    "ProductSubcategoryID" integer NOT NULL,
    "ProductCategoryID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ProductSubcategory_ProductSubcategoryID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ProductSubcategory_ProductSubcategoryID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ProductSubcategory_ProductSubcategoryID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ProductSubcategory_ProductSubcategoryID_seq" OWNED BY "Production"."ProductSubcategory"."ProductSubcategoryID";


--
-- Name: Product_ProductID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."Product_ProductID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Product_ProductID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."Product_ProductID_seq" OWNED BY "Production"."Product"."ProductID";


--
-- Name: ScrapReason; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."ScrapReason" (
    "ScrapReasonID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: ScrapReason_ScrapReasonID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."ScrapReason_ScrapReasonID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ScrapReason_ScrapReasonID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."ScrapReason_ScrapReasonID_seq" OWNED BY "Production"."ScrapReason"."ScrapReasonID";


--
-- Name: TransactionHistory; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."TransactionHistory" (
    "TransactionID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    "ReferenceOrderID" integer NOT NULL,
    "ReferenceOrderLineID" integer DEFAULT 0 NOT NULL,
    "TransactionDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "TransactionType" character(1) NOT NULL,
    "Quantity" integer NOT NULL,
    "ActualCost" numeric(19,4) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_TransactionHistory_TransactionType" CHECK (((upper(("TransactionType")::text) = 'P'::text) OR (upper(("TransactionType")::text) = 'S'::text) OR (upper(("TransactionType")::text) = 'W'::text)))
);


--
-- Name: TransactionHistoryArchive; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."TransactionHistoryArchive" (
    "TransactionID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    "ReferenceOrderID" integer NOT NULL,
    "ReferenceOrderLineID" integer DEFAULT 0 NOT NULL,
    "TransactionDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "TransactionType" character(1) NOT NULL,
    "Quantity" integer NOT NULL,
    "ActualCost" numeric(19,4) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_TransactionHistoryArchive_TransactionType" CHECK (((upper(("TransactionType")::text) = 'P'::text) OR (upper(("TransactionType")::text) = 'S'::text) OR (upper(("TransactionType")::text) = 'W'::text)))
);


--
-- Name: TransactionHistory_TransactionID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."TransactionHistory_TransactionID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: TransactionHistory_TransactionID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."TransactionHistory_TransactionID_seq" OWNED BY "Production"."TransactionHistory"."TransactionID";


--
-- Name: UnitMeasure; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."UnitMeasure" (
    "UnitMeasureCode" character(3) NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: WorkOrder; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."WorkOrder" (
    "WorkOrderID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    "OrderQty" integer NOT NULL,
    "StockedQty" integer,
    "ScrappedQty" smallint NOT NULL,
    "StartDate" timestamp without time zone NOT NULL,
    "EndDate" timestamp without time zone,
    "DueDate" timestamp without time zone NOT NULL,
    "ScrapReasonID" smallint,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_WorkOrder_EndDate" CHECK ((("EndDate" >= "StartDate") OR ("EndDate" IS NULL))),
    CONSTRAINT "CK_WorkOrder_OrderQty" CHECK (("OrderQty" > 0)),
    CONSTRAINT "CK_WorkOrder_ScrappedQty" CHECK (("ScrappedQty" >= 0))
);


--
-- Name: WorkOrderRouting; Type: TABLE; Schema: Production; Owner: -
--

CREATE TABLE "Production"."WorkOrderRouting" (
    "WorkOrderID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    "OperationSequence" smallint NOT NULL,
    "LocationID" smallint NOT NULL,
    "ScheduledStartDate" timestamp without time zone NOT NULL,
    "ScheduledEndDate" timestamp without time zone NOT NULL,
    "ActualStartDate" timestamp without time zone,
    "ActualEndDate" timestamp without time zone,
    "ActualResourceHrs" numeric(9,4),
    "PlannedCost" numeric(19,4) NOT NULL,
    "ActualCost" numeric(19,4),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_WorkOrderRouting_ActualCost" CHECK (("ActualCost" > 0.00)),
    CONSTRAINT "CK_WorkOrderRouting_ActualEndDate" CHECK ((("ActualEndDate" >= "ActualStartDate") OR ("ActualEndDate" IS NULL) OR ("ActualStartDate" IS NULL))),
    CONSTRAINT "CK_WorkOrderRouting_ActualResourceHrs" CHECK (("ActualResourceHrs" >= 0.0000)),
    CONSTRAINT "CK_WorkOrderRouting_PlannedCost" CHECK (("PlannedCost" > 0.00)),
    CONSTRAINT "CK_WorkOrderRouting_ScheduledEndDate" CHECK (("ScheduledEndDate" >= "ScheduledStartDate"))
);


--
-- Name: WorkOrder_WorkOrderID_seq; Type: SEQUENCE; Schema: Production; Owner: -
--

CREATE SEQUENCE "Production"."WorkOrder_WorkOrderID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: WorkOrder_WorkOrderID_seq; Type: SEQUENCE OWNED BY; Schema: Production; Owner: -
--

ALTER SEQUENCE "Production"."WorkOrder_WorkOrderID_seq" OWNED BY "Production"."WorkOrder"."WorkOrderID";


--
-- Name: vProductAndDescription; Type: MATERIALIZED VIEW; Schema: Production; Owner: -
--

CREATE MATERIALIZED VIEW "Production"."vProductAndDescription" AS
 SELECT p."ProductID",
    p."Name",
    pm."Name" AS "ProductModel",
    pmx."CultureID",
    pd."Description"
   FROM ((("Production"."Product" p
     JOIN "Production"."ProductModel" pm ON ((p."ProductModelID" = pm."ProductModelID")))
     JOIN "Production"."ProductModelProductDescriptionCulture" pmx ON ((pm."ProductModelID" = pmx."ProductModelID")))
     JOIN "Production"."ProductDescription" pd ON ((pmx."ProductDescriptionID" = pd."ProductDescriptionID")))
  WITH NO DATA;


--
-- Name: ProductVendor; Type: TABLE; Schema: Purchasing; Owner: -
--

CREATE TABLE "Purchasing"."ProductVendor" (
    "ProductID" integer NOT NULL,
    "BusinessEntityID" integer NOT NULL,
    "AverageLeadTime" integer NOT NULL,
    "StandardPrice" numeric(19,4) NOT NULL,
    "LastReceiptCost" numeric(19,4),
    "LastReceiptDate" timestamp without time zone,
    "MinOrderQty" integer NOT NULL,
    "MaxOrderQty" integer NOT NULL,
    "OnOrderQty" integer,
    "UnitMeasureCode" character(3) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ProductVendor_AverageLeadTime" CHECK (("AverageLeadTime" >= 1)),
    CONSTRAINT "CK_ProductVendor_LastReceiptCost" CHECK (("LastReceiptCost" > 0.00)),
    CONSTRAINT "CK_ProductVendor_MaxOrderQty" CHECK (("MaxOrderQty" >= 1)),
    CONSTRAINT "CK_ProductVendor_MinOrderQty" CHECK (("MinOrderQty" >= 1)),
    CONSTRAINT "CK_ProductVendor_OnOrderQty" CHECK (("OnOrderQty" >= 0)),
    CONSTRAINT "CK_ProductVendor_StandardPrice" CHECK (("StandardPrice" > 0.00))
);


--
-- Name: PurchaseOrderDetail; Type: TABLE; Schema: Purchasing; Owner: -
--

CREATE TABLE "Purchasing"."PurchaseOrderDetail" (
    "PurchaseOrderID" integer NOT NULL,
    "PurchaseOrderDetailID" integer NOT NULL,
    "DueDate" timestamp without time zone NOT NULL,
    "OrderQty" smallint NOT NULL,
    "ProductID" integer NOT NULL,
    "UnitPrice" numeric(19,4) NOT NULL,
    "LineTotal" numeric(19,4),
    "ReceivedQty" numeric(8,2) NOT NULL,
    "RejectedQty" numeric(8,2) NOT NULL,
    "StockedQty" numeric(8,4),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_PurchaseOrderDetail_OrderQty" CHECK (("OrderQty" > 0)),
    CONSTRAINT "CK_PurchaseOrderDetail_ReceivedQty" CHECK (("ReceivedQty" >= 0.00)),
    CONSTRAINT "CK_PurchaseOrderDetail_RejectedQty" CHECK (("RejectedQty" >= 0.00)),
    CONSTRAINT "CK_PurchaseOrderDetail_UnitPrice" CHECK (("UnitPrice" >= 0.00))
);


--
-- Name: PurchaseOrderDetail_PurchaseOrderDetailID_seq; Type: SEQUENCE; Schema: Purchasing; Owner: -
--

CREATE SEQUENCE "Purchasing"."PurchaseOrderDetail_PurchaseOrderDetailID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: PurchaseOrderDetail_PurchaseOrderDetailID_seq; Type: SEQUENCE OWNED BY; Schema: Purchasing; Owner: -
--

ALTER SEQUENCE "Purchasing"."PurchaseOrderDetail_PurchaseOrderDetailID_seq" OWNED BY "Purchasing"."PurchaseOrderDetail"."PurchaseOrderDetailID";


--
-- Name: PurchaseOrderHeader; Type: TABLE; Schema: Purchasing; Owner: -
--

CREATE TABLE "Purchasing"."PurchaseOrderHeader" (
    "PurchaseOrderID" integer NOT NULL,
    "RevisionNumber" smallint DEFAULT 0 NOT NULL,
    "Status" smallint DEFAULT 1 NOT NULL,
    "EmployeeID" integer NOT NULL,
    "VendorID" integer NOT NULL,
    "ShipMethodID" integer NOT NULL,
    "OrderDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "ShipDate" timestamp without time zone,
    "SubTotal" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "TaxAmt" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "Freight" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "TotalDue" numeric(19,4),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_PurchaseOrderHeader_Freight" CHECK (("Freight" >= 0.00)),
    CONSTRAINT "CK_PurchaseOrderHeader_ShipDate" CHECK ((("ShipDate" >= "OrderDate") OR ("ShipDate" IS NULL))),
    CONSTRAINT "CK_PurchaseOrderHeader_Status" CHECK ((("Status" >= 1) AND ("Status" <= 4))),
    CONSTRAINT "CK_PurchaseOrderHeader_SubTotal" CHECK (("SubTotal" >= 0.00)),
    CONSTRAINT "CK_PurchaseOrderHeader_TaxAmt" CHECK (("TaxAmt" >= 0.00))
);


--
-- Name: PurchaseOrderHeader_PurchaseOrderID_seq; Type: SEQUENCE; Schema: Purchasing; Owner: -
--

CREATE SEQUENCE "Purchasing"."PurchaseOrderHeader_PurchaseOrderID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: PurchaseOrderHeader_PurchaseOrderID_seq; Type: SEQUENCE OWNED BY; Schema: Purchasing; Owner: -
--

ALTER SEQUENCE "Purchasing"."PurchaseOrderHeader_PurchaseOrderID_seq" OWNED BY "Purchasing"."PurchaseOrderHeader"."PurchaseOrderID";


--
-- Name: ShipMethod; Type: TABLE; Schema: Purchasing; Owner: -
--

CREATE TABLE "Purchasing"."ShipMethod" (
    "ShipMethodID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ShipBase" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "ShipRate" numeric(19,4) DEFAULT 0.00 NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ShipMethod_ShipBase" CHECK (("ShipBase" > 0.00)),
    CONSTRAINT "CK_ShipMethod_ShipRate" CHECK (("ShipRate" > 0.00))
);


--
-- Name: ShipMethod_ShipMethodID_seq; Type: SEQUENCE; Schema: Purchasing; Owner: -
--

CREATE SEQUENCE "Purchasing"."ShipMethod_ShipMethodID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ShipMethod_ShipMethodID_seq; Type: SEQUENCE OWNED BY; Schema: Purchasing; Owner: -
--

ALTER SEQUENCE "Purchasing"."ShipMethod_ShipMethodID_seq" OWNED BY "Purchasing"."ShipMethod"."ShipMethodID";


--
-- Name: Vendor; Type: TABLE; Schema: Purchasing; Owner: -
--

CREATE TABLE "Purchasing"."Vendor" (
    "BusinessEntityID" integer NOT NULL,
    "AccountNumber" character varying(128) NOT NULL,
    "Name" character varying(256) NOT NULL,
    "CreditRating" smallint NOT NULL,
    "PreferredVendorStatus" boolean DEFAULT true NOT NULL,
    "ActiveFlag" boolean DEFAULT true NOT NULL,
    "PurchasingWebServiceURL" character varying(1024),
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_Vendor_CreditRating" CHECK ((("CreditRating" >= 1) AND ("CreditRating" <= 5)))
);


--
-- Name: vVendorWithAddresses; Type: VIEW; Schema: Purchasing; Owner: -
--

CREATE VIEW "Purchasing"."vVendorWithAddresses" AS
 SELECT v."BusinessEntityID",
    v."Name",
    at."Name" AS "AddressType",
    a."AddressLine1",
    a."AddressLine2",
    a."City",
    sp."Name" AS "StateProvinceName",
    a."PostalCode",
    cr."Name" AS "CountryRegionName"
   FROM ((((("Purchasing"."Vendor" v
     JOIN "Person"."BusinessEntityAddress" bea ON ((bea."BusinessEntityID" = v."BusinessEntityID")))
     JOIN "Person"."Address" a ON ((a."AddressID" = bea."AddressID")))
     JOIN "Person"."StateProvince" sp ON ((sp."StateProvinceID" = a."StateProvinceID")))
     JOIN "Person"."CountryRegion" cr ON (((cr."CountryRegionCode")::text = (sp."CountryRegionCode")::text)))
     JOIN "Person"."AddressType" at ON ((at."AddressTypeID" = bea."AddressTypeID")));


--
-- Name: vVendorWithContacts; Type: VIEW; Schema: Purchasing; Owner: -
--

CREATE VIEW "Purchasing"."vVendorWithContacts" AS
 SELECT v."BusinessEntityID",
    v."Name",
    ct."Name" AS "ContactType",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    pp."PhoneNumber",
    pnt."Name" AS "PhoneNumberType",
    ea."EmailAddress",
    p."EmailPromotion"
   FROM (((((("Purchasing"."Vendor" v
     JOIN "Person"."BusinessEntityContact" bec ON ((bec."BusinessEntityID" = v."BusinessEntityID")))
     JOIN "Person"."ContactType" ct ON ((ct."ContactTypeID" = bec."ContactTypeID")))
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = bec."PersonID")))
     LEFT JOIN "Person"."EmailAddress" ea ON ((ea."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PersonPhone" pp ON ((pp."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PhoneNumberType" pnt ON ((pnt."PhoneNumberTypeID" = pp."PhoneNumberTypeID")));


--
-- Name: CountryRegionCurrency; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."CountryRegionCurrency" (
    "CountryRegionCode" character varying(3) NOT NULL,
    "CurrencyCode" character(3) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: CreditCard; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."CreditCard" (
    "CreditCardID" integer NOT NULL,
    "CardType" character varying(50) NOT NULL,
    "CardNumber" character varying(25) NOT NULL,
    "ExpMonth" smallint NOT NULL,
    "ExpYear" smallint NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: CreditCard_CreditCardID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."CreditCard_CreditCardID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: CreditCard_CreditCardID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."CreditCard_CreditCardID_seq" OWNED BY "Sales"."CreditCard"."CreditCardID";


--
-- Name: Currency; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."Currency" (
    "CurrencyCode" character(3) NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: CurrencyRate; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."CurrencyRate" (
    "CurrencyRateID" integer NOT NULL,
    "CurrencyRateDate" timestamp without time zone NOT NULL,
    "FromCurrencyCode" character(3) NOT NULL,
    "ToCurrencyCode" character(3) NOT NULL,
    "AverageRate" numeric(19,4) NOT NULL,
    "EndOfDayRate" numeric(19,4) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: CurrencyRate_CurrencyRateID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."CurrencyRate_CurrencyRateID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: CurrencyRate_CurrencyRateID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."CurrencyRate_CurrencyRateID_seq" OWNED BY "Sales"."CurrencyRate"."CurrencyRateID";


--
-- Name: Customer; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."Customer" (
    "CustomerID" integer NOT NULL,
    "PersonID" integer,
    "StoreID" integer,
    "TerritoryID" integer,
    "AccountNumber" character varying(128),
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: Customer_CustomerID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."Customer_CustomerID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: Customer_CustomerID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."Customer_CustomerID_seq" OWNED BY "Sales"."Customer"."CustomerID";


--
-- Name: PersonCreditCard; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."PersonCreditCard" (
    "BusinessEntityID" integer NOT NULL,
    "CreditCardID" integer NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: SalesOrderDetail; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesOrderDetail" (
    "SalesOrderID" integer NOT NULL,
    "SalesOrderDetailID" integer NOT NULL,
    "CarrierTrackingNumber" character varying(25),
    "OrderQty" smallint NOT NULL,
    "ProductID" integer NOT NULL,
    "SpecialOfferID" integer NOT NULL,
    "UnitPrice" numeric(19,4) NOT NULL,
    "UnitPriceDiscount" numeric(19,4) DEFAULT 0.0 NOT NULL,
    "LineTotal" numeric(19,4),
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesOrderDetail_OrderQty" CHECK (("OrderQty" > 0)),
    CONSTRAINT "CK_SalesOrderDetail_UnitPrice" CHECK (("UnitPrice" >= 0.00)),
    CONSTRAINT "CK_SalesOrderDetail_UnitPriceDiscount" CHECK (("UnitPriceDiscount" >= 0.00))
);


--
-- Name: SalesOrderDetail_SalesOrderDetailID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SalesOrderDetail_SalesOrderDetailID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SalesOrderDetail_SalesOrderDetailID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SalesOrderDetail_SalesOrderDetailID_seq" OWNED BY "Sales"."SalesOrderDetail"."SalesOrderDetailID";


--
-- Name: SalesOrderHeader; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesOrderHeader" (
    "SalesOrderID" integer NOT NULL,
    "RevisionNumber" smallint DEFAULT 0 NOT NULL,
    "OrderDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "DueDate" timestamp without time zone NOT NULL,
    "ShipDate" timestamp without time zone,
    "Status" smallint DEFAULT 1 NOT NULL,
    "OnlineOrderFlag" boolean DEFAULT true NOT NULL,
    "SalesOrderNumber" character varying(128),
    "PurchaseOrderNumber" character varying(128),
    "AccountNumber" character varying(128),
    "CustomerID" integer NOT NULL,
    "SalesPersonID" integer,
    "TerritoryID" integer,
    "BillToAddressID" integer NOT NULL,
    "ShipToAddressID" integer NOT NULL,
    "ShipMethodID" integer NOT NULL,
    "CreditCardID" integer,
    "CreditCardApprovalCode" character varying(15),
    "CurrencyRateID" integer,
    "SubTotal" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "TaxAmt" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "Freight" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "TotalDue" numeric(19,4),
    "Comment" character varying(128),
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesOrderHeader_DueDate" CHECK (("DueDate" >= "OrderDate")),
    CONSTRAINT "CK_SalesOrderHeader_Freight" CHECK (("Freight" >= 0.00)),
    CONSTRAINT "CK_SalesOrderHeader_ShipDate" CHECK ((("ShipDate" >= "OrderDate") OR ("ShipDate" IS NULL))),
    CONSTRAINT "CK_SalesOrderHeader_Status" CHECK ((("Status" >= 0) AND ("Status" <= 8))),
    CONSTRAINT "CK_SalesOrderHeader_SubTotal" CHECK (("SubTotal" >= 0.00)),
    CONSTRAINT "CK_SalesOrderHeader_TaxAmt" CHECK (("TaxAmt" >= 0.00))
);


--
-- Name: SalesOrderHeaderSalesReason; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesOrderHeaderSalesReason" (
    "SalesOrderID" integer NOT NULL,
    "SalesReasonID" integer NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: SalesOrderHeader_SalesOrderID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SalesOrderHeader_SalesOrderID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SalesOrderHeader_SalesOrderID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SalesOrderHeader_SalesOrderID_seq" OWNED BY "Sales"."SalesOrderHeader"."SalesOrderID";


--
-- Name: SalesPerson; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesPerson" (
    "BusinessEntityID" integer NOT NULL,
    "TerritoryID" integer,
    "SalesQuota" numeric(19,4),
    "Bonus" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "CommissionPct" numeric(8,2) DEFAULT 0.00 NOT NULL,
    "SalesYTD" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "SalesLastYear" numeric(19,4) DEFAULT 0.00 NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesPerson_Bonus" CHECK (("Bonus" >= 0.00)),
    CONSTRAINT "CK_SalesPerson_CommissionPct" CHECK (("CommissionPct" >= 0.00)),
    CONSTRAINT "CK_SalesPerson_SalesLastYear" CHECK (("SalesLastYear" >= 0.00)),
    CONSTRAINT "CK_SalesPerson_SalesQuota" CHECK (("SalesQuota" > 0.00)),
    CONSTRAINT "CK_SalesPerson_SalesYTD" CHECK (("SalesYTD" >= 0.00))
);


--
-- Name: SalesPersonQuotaHistory; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesPersonQuotaHistory" (
    "BusinessEntityID" integer NOT NULL,
    "QuotaDate" timestamp without time zone NOT NULL,
    "SalesQuota" numeric(19,4) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesPersonQuotaHistory_SalesQuota" CHECK (("SalesQuota" > 0.00))
);


--
-- Name: SalesReason; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesReason" (
    "SalesReasonID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "ReasonType" character varying(256) NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: SalesReason_SalesReasonID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SalesReason_SalesReasonID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SalesReason_SalesReasonID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SalesReason_SalesReasonID_seq" OWNED BY "Sales"."SalesReason"."SalesReasonID";


--
-- Name: SalesTaxRate; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesTaxRate" (
    "SalesTaxRateID" integer NOT NULL,
    "StateProvinceID" integer NOT NULL,
    "TaxType" smallint NOT NULL,
    "TaxRate" numeric(8,2) DEFAULT 0.00 NOT NULL,
    "Name" character varying(256) NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesTaxRate_TaxType" CHECK ((("TaxType" >= 1) AND ("TaxType" <= 3)))
);


--
-- Name: SalesTaxRate_SalesTaxRateID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SalesTaxRate_SalesTaxRateID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SalesTaxRate_SalesTaxRateID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SalesTaxRate_SalesTaxRateID_seq" OWNED BY "Sales"."SalesTaxRate"."SalesTaxRateID";


--
-- Name: SalesTerritory; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesTerritory" (
    "TerritoryID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "CountryRegionCode" character varying(3) NOT NULL,
    "Group" character varying(50) NOT NULL,
    "SalesYTD" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "SalesLastYear" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "CostYTD" numeric(19,4) DEFAULT 0.00 NOT NULL,
    "CostLastYear" numeric(19,4) DEFAULT 0.00 NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesTerritory_CostLastYear" CHECK (("CostLastYear" >= 0.00)),
    CONSTRAINT "CK_SalesTerritory_CostYTD" CHECK (("CostYTD" >= 0.00)),
    CONSTRAINT "CK_SalesTerritory_SalesLastYear" CHECK (("SalesLastYear" >= 0.00)),
    CONSTRAINT "CK_SalesTerritory_SalesYTD" CHECK (("SalesYTD" >= 0.00))
);


--
-- Name: SalesTerritoryHistory; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SalesTerritoryHistory" (
    "BusinessEntityID" integer NOT NULL,
    "TerritoryID" integer NOT NULL,
    "StartDate" timestamp without time zone NOT NULL,
    "EndDate" timestamp without time zone,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SalesTerritoryHistory_EndDate" CHECK ((("EndDate" >= "StartDate") OR ("EndDate" IS NULL)))
);


--
-- Name: SalesTerritory_TerritoryID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SalesTerritory_TerritoryID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SalesTerritory_TerritoryID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SalesTerritory_TerritoryID_seq" OWNED BY "Sales"."SalesTerritory"."TerritoryID";


--
-- Name: ShoppingCartItem; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."ShoppingCartItem" (
    "ShoppingCartItemID" integer NOT NULL,
    "ShoppingCartID" character varying(50) NOT NULL,
    "Quantity" integer DEFAULT 1 NOT NULL,
    "ProductID" integer NOT NULL,
    "DateCreated" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_ShoppingCartItem_Quantity" CHECK (("Quantity" >= 1))
);


--
-- Name: ShoppingCartItem_ShoppingCartItemID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."ShoppingCartItem_ShoppingCartItemID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ShoppingCartItem_ShoppingCartItemID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."ShoppingCartItem_ShoppingCartItemID_seq" OWNED BY "Sales"."ShoppingCartItem"."ShoppingCartItemID";


--
-- Name: SpecialOffer; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SpecialOffer" (
    "SpecialOfferID" integer NOT NULL,
    "Description" character varying(255) NOT NULL,
    "DiscountPct" numeric(8,2) DEFAULT 0.00 NOT NULL,
    "Type" character varying(50) NOT NULL,
    "Category" character varying(50) NOT NULL,
    "StartDate" timestamp without time zone NOT NULL,
    "EndDate" timestamp without time zone NOT NULL,
    "MinQty" integer DEFAULT 0 NOT NULL,
    "MaxQty" integer,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    CONSTRAINT "CK_SpecialOffer_DiscountPct" CHECK (("DiscountPct" >= 0.00)),
    CONSTRAINT "CK_SpecialOffer_EndDate" CHECK (("EndDate" >= "StartDate")),
    CONSTRAINT "CK_SpecialOffer_MaxQty" CHECK (("MaxQty" >= 0)),
    CONSTRAINT "CK_SpecialOffer_MinQty" CHECK (("MinQty" >= 0))
);


--
-- Name: SpecialOfferProduct; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."SpecialOfferProduct" (
    "SpecialOfferID" integer NOT NULL,
    "ProductID" integer NOT NULL,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: SpecialOffer_SpecialOfferID_seq; Type: SEQUENCE; Schema: Sales; Owner: -
--

CREATE SEQUENCE "Sales"."SpecialOffer_SpecialOfferID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: SpecialOffer_SpecialOfferID_seq; Type: SEQUENCE OWNED BY; Schema: Sales; Owner: -
--

ALTER SEQUENCE "Sales"."SpecialOffer_SpecialOfferID_seq" OWNED BY "Sales"."SpecialOffer"."SpecialOfferID";


--
-- Name: Store; Type: TABLE; Schema: Sales; Owner: -
--

CREATE TABLE "Sales"."Store" (
    "BusinessEntityID" integer NOT NULL,
    "Name" character varying(256) NOT NULL,
    "SalesPersonID" integer,
    "Demographics" xml,
    rowguid uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: vIndividualCustomer; Type: VIEW; Schema: Sales; Owner: -
--

CREATE VIEW "Sales"."vIndividualCustomer" AS
 SELECT p."BusinessEntityID",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    pp."PhoneNumber",
    pnt."Name" AS "PhoneNumberType",
    ea."EmailAddress",
    p."EmailPromotion",
    at."Name" AS "AddressType",
    a."AddressLine1",
    a."AddressLine2",
    a."City",
    sp."Name" AS "StateProvinceName",
    a."PostalCode",
    cr."Name" AS "CountryRegionName",
    p."Demographics"
   FROM ((((((((("Person"."Person" p
     JOIN "Person"."BusinessEntityAddress" bea ON ((bea."BusinessEntityID" = p."BusinessEntityID")))
     JOIN "Person"."Address" a ON ((a."AddressID" = bea."AddressID")))
     JOIN "Person"."StateProvince" sp ON ((sp."StateProvinceID" = a."StateProvinceID")))
     JOIN "Person"."CountryRegion" cr ON (((cr."CountryRegionCode")::text = (sp."CountryRegionCode")::text)))
     JOIN "Person"."AddressType" at ON ((at."AddressTypeID" = bea."AddressTypeID")))
     JOIN "Sales"."Customer" c ON ((c."PersonID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."EmailAddress" ea ON ((ea."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PersonPhone" pp ON ((pp."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PhoneNumberType" pnt ON ((pnt."PhoneNumberTypeID" = pp."PhoneNumberTypeID")))
  WHERE (c."StoreID" IS NULL);


--
-- Name: vSalesPerson; Type: VIEW; Schema: Sales; Owner: -
--

CREATE VIEW "Sales"."vSalesPerson" AS
 SELECT s."BusinessEntityID",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    e."JobTitle",
    pp."PhoneNumber",
    pnt."Name" AS "PhoneNumberType",
    ea."EmailAddress",
    p."EmailPromotion",
    a."AddressLine1",
    a."AddressLine2",
    a."City",
    sp."Name" AS "StateProvinceName",
    a."PostalCode",
    cr."Name" AS "CountryRegionName",
    st."Name" AS "TerritoryName",
    st."Group" AS "TerritoryGroup",
    s."SalesQuota",
    s."SalesYTD",
    s."SalesLastYear"
   FROM (((((((((("Sales"."SalesPerson" s
     JOIN "HumanResources"."Employee" e ON ((e."BusinessEntityID" = s."BusinessEntityID")))
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = s."BusinessEntityID")))
     JOIN "Person"."BusinessEntityAddress" bea ON ((bea."BusinessEntityID" = s."BusinessEntityID")))
     JOIN "Person"."Address" a ON ((a."AddressID" = bea."AddressID")))
     JOIN "Person"."StateProvince" sp ON ((sp."StateProvinceID" = a."StateProvinceID")))
     JOIN "Person"."CountryRegion" cr ON (((cr."CountryRegionCode")::text = (sp."CountryRegionCode")::text)))
     LEFT JOIN "Sales"."SalesTerritory" st ON ((st."TerritoryID" = s."TerritoryID")))
     LEFT JOIN "Person"."EmailAddress" ea ON ((ea."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PersonPhone" pp ON ((pp."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PhoneNumberType" pnt ON ((pnt."PhoneNumberTypeID" = pp."PhoneNumberTypeID")));


--
-- Name: vStoreWithAddresses; Type: VIEW; Schema: Sales; Owner: -
--

CREATE VIEW "Sales"."vStoreWithAddresses" AS
 SELECT s."BusinessEntityID",
    s."Name",
    at."Name" AS "AddressType",
    a."AddressLine1",
    a."AddressLine2",
    a."City",
    sp."Name" AS "StateProvinceName",
    a."PostalCode",
    cr."Name" AS "CountryRegionName"
   FROM ((((("Sales"."Store" s
     JOIN "Person"."BusinessEntityAddress" bea ON ((bea."BusinessEntityID" = s."BusinessEntityID")))
     JOIN "Person"."Address" a ON ((a."AddressID" = bea."AddressID")))
     JOIN "Person"."StateProvince" sp ON ((sp."StateProvinceID" = a."StateProvinceID")))
     JOIN "Person"."CountryRegion" cr ON (((cr."CountryRegionCode")::text = (sp."CountryRegionCode")::text)))
     JOIN "Person"."AddressType" at ON ((at."AddressTypeID" = bea."AddressTypeID")));


--
-- Name: vStoreWithContacts; Type: VIEW; Schema: Sales; Owner: -
--

CREATE VIEW "Sales"."vStoreWithContacts" AS
 SELECT s."BusinessEntityID",
    s."Name",
    ct."Name" AS "ContactType",
    p."Title",
    p."FirstName",
    p."MiddleName",
    p."LastName",
    p."Suffix",
    pp."PhoneNumber",
    pnt."Name" AS "PhoneNumberType",
    ea."EmailAddress",
    p."EmailPromotion"
   FROM (((((("Sales"."Store" s
     JOIN "Person"."BusinessEntityContact" bec ON ((bec."BusinessEntityID" = s."BusinessEntityID")))
     JOIN "Person"."ContactType" ct ON ((ct."ContactTypeID" = bec."ContactTypeID")))
     JOIN "Person"."Person" p ON ((p."BusinessEntityID" = bec."PersonID")))
     LEFT JOIN "Person"."EmailAddress" ea ON ((ea."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PersonPhone" pp ON ((pp."BusinessEntityID" = p."BusinessEntityID")))
     LEFT JOIN "Person"."PhoneNumberType" pnt ON ((pnt."PhoneNumberTypeID" = pp."PhoneNumberTypeID")));


--
-- Name: AWBuildVersion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."AWBuildVersion" (
    "SystemInformationID" integer NOT NULL,
    "Database Version" character varying(25) NOT NULL,
    "VersionDate" timestamp without time zone NOT NULL,
    "ModifiedDate" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: AWBuildVersion_SystemInformationID_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."AWBuildVersion_SystemInformationID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: AWBuildVersion_SystemInformationID_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."AWBuildVersion_SystemInformationID_seq" OWNED BY public."AWBuildVersion"."SystemInformationID";


--
-- Name: DatabaseLog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."DatabaseLog" (
    "DatabaseLogID" integer NOT NULL,
    "PostTime" timestamp without time zone NOT NULL,
    "DatabaseUser" character varying(256) NOT NULL,
    "Event" character varying(256) NOT NULL,
    "Schema" character varying(256),
    "Object" character varying(256),
    "TSQL" text NOT NULL,
    "XmlEvent" xml NOT NULL
);


--
-- Name: DatabaseLog_DatabaseLogID_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."DatabaseLog_DatabaseLogID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: DatabaseLog_DatabaseLogID_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."DatabaseLog_DatabaseLogID_seq" OWNED BY public."DatabaseLog"."DatabaseLogID";


--
-- Name: ErrorLog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."ErrorLog" (
    "ErrorLogID" integer NOT NULL,
    "ErrorTime" timestamp without time zone DEFAULT CURRENT_DATE NOT NULL,
    "UserName" character varying(256) NOT NULL,
    "ErrorNumber" integer NOT NULL,
    "ErrorSeverity" integer,
    "ErrorState" integer,
    "ErrorProcedure" character varying(126),
    "ErrorLine" integer,
    "ErrorMessage" character varying(4000) NOT NULL
);


--
-- Name: ErrorLog_ErrorLogID_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public."ErrorLog_ErrorLogID_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ErrorLog_ErrorLogID_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public."ErrorLog_ErrorLogID_seq" OWNED BY public."ErrorLog"."ErrorLogID";


--
-- Name: Department DepartmentID; Type: DEFAULT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Department" ALTER COLUMN "DepartmentID" SET DEFAULT nextval('"HumanResources"."Department_DepartmentID_seq"'::regclass);


--
-- Name: JobCandidate JobCandidateID; Type: DEFAULT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."JobCandidate" ALTER COLUMN "JobCandidateID" SET DEFAULT nextval('"HumanResources"."JobCandidate_JobCandidateID_seq"'::regclass);


--
-- Name: Shift ShiftID; Type: DEFAULT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Shift" ALTER COLUMN "ShiftID" SET DEFAULT nextval('"HumanResources"."Shift_ShiftID_seq"'::regclass);


--
-- Name: Address AddressID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Address" ALTER COLUMN "AddressID" SET DEFAULT nextval('"Person"."Address_AddressID_seq"'::regclass);


--
-- Name: AddressType AddressTypeID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."AddressType" ALTER COLUMN "AddressTypeID" SET DEFAULT nextval('"Person"."AddressType_AddressTypeID_seq"'::regclass);


--
-- Name: BusinessEntity BusinessEntityID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntity" ALTER COLUMN "BusinessEntityID" SET DEFAULT nextval('"Person"."BusinessEntity_BusinessEntityID_seq"'::regclass);


--
-- Name: ContactType ContactTypeID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."ContactType" ALTER COLUMN "ContactTypeID" SET DEFAULT nextval('"Person"."ContactType_ContactTypeID_seq"'::regclass);


--
-- Name: EmailAddress EmailAddressID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."EmailAddress" ALTER COLUMN "EmailAddressID" SET DEFAULT nextval('"Person"."EmailAddress_EmailAddressID_seq"'::regclass);


--
-- Name: PhoneNumberType PhoneNumberTypeID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."PhoneNumberType" ALTER COLUMN "PhoneNumberTypeID" SET DEFAULT nextval('"Person"."PhoneNumberType_PhoneNumberTypeID_seq"'::regclass);


--
-- Name: StateProvince StateProvinceID; Type: DEFAULT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."StateProvince" ALTER COLUMN "StateProvinceID" SET DEFAULT nextval('"Person"."StateProvince_StateProvinceID_seq"'::regclass);


--
-- Name: BillOfMaterials BillOfMaterialsID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."BillOfMaterials" ALTER COLUMN "BillOfMaterialsID" SET DEFAULT nextval('"Production"."BillOfMaterials_BillOfMaterialsID_seq"'::regclass);


--
-- Name: Illustration IllustrationID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Illustration" ALTER COLUMN "IllustrationID" SET DEFAULT nextval('"Production"."Illustration_IllustrationID_seq"'::regclass);


--
-- Name: Location LocationID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Location" ALTER COLUMN "LocationID" SET DEFAULT nextval('"Production"."Location_LocationID_seq"'::regclass);


--
-- Name: Product ProductID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product" ALTER COLUMN "ProductID" SET DEFAULT nextval('"Production"."Product_ProductID_seq"'::regclass);


--
-- Name: ProductCategory ProductCategoryID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductCategory" ALTER COLUMN "ProductCategoryID" SET DEFAULT nextval('"Production"."ProductCategory_ProductCategoryID_seq"'::regclass);


--
-- Name: ProductDescription ProductDescriptionID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductDescription" ALTER COLUMN "ProductDescriptionID" SET DEFAULT nextval('"Production"."ProductDescription_ProductDescriptionID_seq"'::regclass);


--
-- Name: ProductModel ProductModelID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModel" ALTER COLUMN "ProductModelID" SET DEFAULT nextval('"Production"."ProductModel_ProductModelID_seq"'::regclass);


--
-- Name: ProductPhoto ProductPhotoID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductPhoto" ALTER COLUMN "ProductPhotoID" SET DEFAULT nextval('"Production"."ProductPhoto_ProductPhotoID_seq"'::regclass);


--
-- Name: ProductReview ProductReviewID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductReview" ALTER COLUMN "ProductReviewID" SET DEFAULT nextval('"Production"."ProductReview_ProductReviewID_seq"'::regclass);


--
-- Name: ProductSubcategory ProductSubcategoryID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductSubcategory" ALTER COLUMN "ProductSubcategoryID" SET DEFAULT nextval('"Production"."ProductSubcategory_ProductSubcategoryID_seq"'::regclass);


--
-- Name: ScrapReason ScrapReasonID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ScrapReason" ALTER COLUMN "ScrapReasonID" SET DEFAULT nextval('"Production"."ScrapReason_ScrapReasonID_seq"'::regclass);


--
-- Name: TransactionHistory TransactionID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."TransactionHistory" ALTER COLUMN "TransactionID" SET DEFAULT nextval('"Production"."TransactionHistory_TransactionID_seq"'::regclass);


--
-- Name: WorkOrder WorkOrderID; Type: DEFAULT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrder" ALTER COLUMN "WorkOrderID" SET DEFAULT nextval('"Production"."WorkOrder_WorkOrderID_seq"'::regclass);


--
-- Name: PurchaseOrderDetail PurchaseOrderDetailID; Type: DEFAULT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderDetail" ALTER COLUMN "PurchaseOrderDetailID" SET DEFAULT nextval('"Purchasing"."PurchaseOrderDetail_PurchaseOrderDetailID_seq"'::regclass);


--
-- Name: PurchaseOrderHeader PurchaseOrderID; Type: DEFAULT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderHeader" ALTER COLUMN "PurchaseOrderID" SET DEFAULT nextval('"Purchasing"."PurchaseOrderHeader_PurchaseOrderID_seq"'::regclass);


--
-- Name: ShipMethod ShipMethodID; Type: DEFAULT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ShipMethod" ALTER COLUMN "ShipMethodID" SET DEFAULT nextval('"Purchasing"."ShipMethod_ShipMethodID_seq"'::regclass);


--
-- Name: CreditCard CreditCardID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CreditCard" ALTER COLUMN "CreditCardID" SET DEFAULT nextval('"Sales"."CreditCard_CreditCardID_seq"'::regclass);


--
-- Name: CurrencyRate CurrencyRateID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CurrencyRate" ALTER COLUMN "CurrencyRateID" SET DEFAULT nextval('"Sales"."CurrencyRate_CurrencyRateID_seq"'::regclass);


--
-- Name: Customer CustomerID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Customer" ALTER COLUMN "CustomerID" SET DEFAULT nextval('"Sales"."Customer_CustomerID_seq"'::regclass);


--
-- Name: SalesOrderDetail SalesOrderDetailID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderDetail" ALTER COLUMN "SalesOrderDetailID" SET DEFAULT nextval('"Sales"."SalesOrderDetail_SalesOrderDetailID_seq"'::regclass);


--
-- Name: SalesOrderHeader SalesOrderID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader" ALTER COLUMN "SalesOrderID" SET DEFAULT nextval('"Sales"."SalesOrderHeader_SalesOrderID_seq"'::regclass);


--
-- Name: SalesReason SalesReasonID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesReason" ALTER COLUMN "SalesReasonID" SET DEFAULT nextval('"Sales"."SalesReason_SalesReasonID_seq"'::regclass);


--
-- Name: SalesTaxRate SalesTaxRateID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTaxRate" ALTER COLUMN "SalesTaxRateID" SET DEFAULT nextval('"Sales"."SalesTaxRate_SalesTaxRateID_seq"'::regclass);


--
-- Name: SalesTerritory TerritoryID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritory" ALTER COLUMN "TerritoryID" SET DEFAULT nextval('"Sales"."SalesTerritory_TerritoryID_seq"'::regclass);


--
-- Name: ShoppingCartItem ShoppingCartItemID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."ShoppingCartItem" ALTER COLUMN "ShoppingCartItemID" SET DEFAULT nextval('"Sales"."ShoppingCartItem_ShoppingCartItemID_seq"'::regclass);


--
-- Name: SpecialOffer SpecialOfferID; Type: DEFAULT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SpecialOffer" ALTER COLUMN "SpecialOfferID" SET DEFAULT nextval('"Sales"."SpecialOffer_SpecialOfferID_seq"'::regclass);


--
-- Name: AWBuildVersion SystemInformationID; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."AWBuildVersion" ALTER COLUMN "SystemInformationID" SET DEFAULT nextval('public."AWBuildVersion_SystemInformationID_seq"'::regclass);


--
-- Name: DatabaseLog DatabaseLogID; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."DatabaseLog" ALTER COLUMN "DatabaseLogID" SET DEFAULT nextval('public."DatabaseLog_DatabaseLogID_seq"'::regclass);


--
-- Name: ErrorLog ErrorLogID; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ErrorLog" ALTER COLUMN "ErrorLogID" SET DEFAULT nextval('public."ErrorLog_ErrorLogID_seq"'::regclass);


--
-- Name: Department PK_Department_DepartmentID; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Department"
    ADD CONSTRAINT "PK_Department_DepartmentID" PRIMARY KEY ("DepartmentID");


--
-- Name: EmployeeDepartmentHistory PK_EmployeeDepartmentHistory_BusinessEntityID_StartDate_Departm; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeeDepartmentHistory"
    ADD CONSTRAINT "PK_EmployeeDepartmentHistory_BusinessEntityID_StartDate_Departm" PRIMARY KEY ("BusinessEntityID", "StartDate", "DepartmentID", "ShiftID");


--
-- Name: EmployeePayHistory PK_EmployeePayHistory_BusinessEntityID_RateChangeDate; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeePayHistory"
    ADD CONSTRAINT "PK_EmployeePayHistory_BusinessEntityID_RateChangeDate" PRIMARY KEY ("BusinessEntityID", "RateChangeDate");


--
-- Name: Employee PK_Employee_BusinessEntityID; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Employee"
    ADD CONSTRAINT "PK_Employee_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: JobCandidate PK_JobCandidate_JobCandidateID; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."JobCandidate"
    ADD CONSTRAINT "PK_JobCandidate_JobCandidateID" PRIMARY KEY ("JobCandidateID");


--
-- Name: Shift PK_Shift_ShiftID; Type: CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Shift"
    ADD CONSTRAINT "PK_Shift_ShiftID" PRIMARY KEY ("ShiftID");


--
-- Name: AddressType PK_AddressType_AddressTypeID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."AddressType"
    ADD CONSTRAINT "PK_AddressType_AddressTypeID" PRIMARY KEY ("AddressTypeID");


--
-- Name: Address PK_Address_AddressID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Address"
    ADD CONSTRAINT "PK_Address_AddressID" PRIMARY KEY ("AddressID");


--
-- Name: BusinessEntityAddress PK_BusinessEntityAddress_BusinessEntityID_AddressID_AddressType; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityAddress"
    ADD CONSTRAINT "PK_BusinessEntityAddress_BusinessEntityID_AddressID_AddressType" PRIMARY KEY ("BusinessEntityID", "AddressID", "AddressTypeID");


--
-- Name: BusinessEntityContact PK_BusinessEntityContact_BusinessEntityID_PersonID_ContactTypeI; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityContact"
    ADD CONSTRAINT "PK_BusinessEntityContact_BusinessEntityID_PersonID_ContactTypeI" PRIMARY KEY ("BusinessEntityID", "PersonID", "ContactTypeID");


--
-- Name: BusinessEntity PK_BusinessEntity_BusinessEntityID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntity"
    ADD CONSTRAINT "PK_BusinessEntity_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: ContactType PK_ContactType_ContactTypeID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."ContactType"
    ADD CONSTRAINT "PK_ContactType_ContactTypeID" PRIMARY KEY ("ContactTypeID");


--
-- Name: CountryRegion PK_CountryRegion_CountryRegionCode; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."CountryRegion"
    ADD CONSTRAINT "PK_CountryRegion_CountryRegionCode" PRIMARY KEY ("CountryRegionCode");


--
-- Name: EmailAddress PK_EmailAddress_BusinessEntityID_EmailAddressID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."EmailAddress"
    ADD CONSTRAINT "PK_EmailAddress_BusinessEntityID_EmailAddressID" PRIMARY KEY ("BusinessEntityID", "EmailAddressID");


--
-- Name: Password PK_Password_BusinessEntityID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Password"
    ADD CONSTRAINT "PK_Password_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: PersonPhone PK_PersonPhone_BusinessEntityID_PhoneNumber_PhoneNumberTypeID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."PersonPhone"
    ADD CONSTRAINT "PK_PersonPhone_BusinessEntityID_PhoneNumber_PhoneNumberTypeID" PRIMARY KEY ("BusinessEntityID", "PhoneNumber", "PhoneNumberTypeID");


--
-- Name: Person PK_Person_BusinessEntityID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Person"
    ADD CONSTRAINT "PK_Person_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: PhoneNumberType PK_PhoneNumberType_PhoneNumberTypeID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."PhoneNumberType"
    ADD CONSTRAINT "PK_PhoneNumberType_PhoneNumberTypeID" PRIMARY KEY ("PhoneNumberTypeID");


--
-- Name: StateProvince PK_StateProvince_StateProvinceID; Type: CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."StateProvince"
    ADD CONSTRAINT "PK_StateProvince_StateProvinceID" PRIMARY KEY ("StateProvinceID");


--
-- Name: Document Document_rowguid_key; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Document"
    ADD CONSTRAINT "Document_rowguid_key" UNIQUE (rowguid);


--
-- Name: BillOfMaterials PK_BillOfMaterials_BillOfMaterialsID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."BillOfMaterials"
    ADD CONSTRAINT "PK_BillOfMaterials_BillOfMaterialsID" PRIMARY KEY ("BillOfMaterialsID");


--
-- Name: Culture PK_Culture_CultureID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Culture"
    ADD CONSTRAINT "PK_Culture_CultureID" PRIMARY KEY ("CultureID");


--
-- Name: Document PK_Document_DocumentNode; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Document"
    ADD CONSTRAINT "PK_Document_DocumentNode" PRIMARY KEY ("DocumentNode");


--
-- Name: Illustration PK_Illustration_IllustrationID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Illustration"
    ADD CONSTRAINT "PK_Illustration_IllustrationID" PRIMARY KEY ("IllustrationID");


--
-- Name: Location PK_Location_LocationID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Location"
    ADD CONSTRAINT "PK_Location_LocationID" PRIMARY KEY ("LocationID");


--
-- Name: ProductCategory PK_ProductCategory_ProductCategoryID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductCategory"
    ADD CONSTRAINT "PK_ProductCategory_ProductCategoryID" PRIMARY KEY ("ProductCategoryID");


--
-- Name: ProductCostHistory PK_ProductCostHistory_ProductID_StartDate; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductCostHistory"
    ADD CONSTRAINT "PK_ProductCostHistory_ProductID_StartDate" PRIMARY KEY ("ProductID", "StartDate");


--
-- Name: ProductDescription PK_ProductDescription_ProductDescriptionID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductDescription"
    ADD CONSTRAINT "PK_ProductDescription_ProductDescriptionID" PRIMARY KEY ("ProductDescriptionID");


--
-- Name: ProductDocument PK_ProductDocument_ProductID_DocumentNode; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductDocument"
    ADD CONSTRAINT "PK_ProductDocument_ProductID_DocumentNode" PRIMARY KEY ("ProductID", "DocumentNode");


--
-- Name: ProductInventory PK_ProductInventory_ProductID_LocationID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductInventory"
    ADD CONSTRAINT "PK_ProductInventory_ProductID_LocationID" PRIMARY KEY ("ProductID", "LocationID");


--
-- Name: ProductListPriceHistory PK_ProductListPriceHistory_ProductID_StartDate; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductListPriceHistory"
    ADD CONSTRAINT "PK_ProductListPriceHistory_ProductID_StartDate" PRIMARY KEY ("ProductID", "StartDate");


--
-- Name: ProductModelIllustration PK_ProductModelIllustration_ProductModelID_IllustrationID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelIllustration"
    ADD CONSTRAINT "PK_ProductModelIllustration_ProductModelID_IllustrationID" PRIMARY KEY ("ProductModelID", "IllustrationID");


--
-- Name: ProductModelProductDescriptionCulture PK_ProductModelProductDescriptionCulture_ProductModelID_Product; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelProductDescriptionCulture"
    ADD CONSTRAINT "PK_ProductModelProductDescriptionCulture_ProductModelID_Product" PRIMARY KEY ("ProductModelID", "ProductDescriptionID", "CultureID");


--
-- Name: ProductModel PK_ProductModel_ProductModelID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModel"
    ADD CONSTRAINT "PK_ProductModel_ProductModelID" PRIMARY KEY ("ProductModelID");


--
-- Name: ProductPhoto PK_ProductPhoto_ProductPhotoID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductPhoto"
    ADD CONSTRAINT "PK_ProductPhoto_ProductPhotoID" PRIMARY KEY ("ProductPhotoID");


--
-- Name: ProductProductPhoto PK_ProductProductPhoto_ProductID_ProductPhotoID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductProductPhoto"
    ADD CONSTRAINT "PK_ProductProductPhoto_ProductID_ProductPhotoID" PRIMARY KEY ("ProductID", "ProductPhotoID");


--
-- Name: ProductReview PK_ProductReview_ProductReviewID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductReview"
    ADD CONSTRAINT "PK_ProductReview_ProductReviewID" PRIMARY KEY ("ProductReviewID");


--
-- Name: ProductSubcategory PK_ProductSubcategory_ProductSubcategoryID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductSubcategory"
    ADD CONSTRAINT "PK_ProductSubcategory_ProductSubcategoryID" PRIMARY KEY ("ProductSubcategoryID");


--
-- Name: Product PK_Product_ProductID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product"
    ADD CONSTRAINT "PK_Product_ProductID" PRIMARY KEY ("ProductID");


--
-- Name: ScrapReason PK_ScrapReason_ScrapReasonID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ScrapReason"
    ADD CONSTRAINT "PK_ScrapReason_ScrapReasonID" PRIMARY KEY ("ScrapReasonID");


--
-- Name: TransactionHistoryArchive PK_TransactionHistoryArchive_TransactionID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."TransactionHistoryArchive"
    ADD CONSTRAINT "PK_TransactionHistoryArchive_TransactionID" PRIMARY KEY ("TransactionID");


--
-- Name: TransactionHistory PK_TransactionHistory_TransactionID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."TransactionHistory"
    ADD CONSTRAINT "PK_TransactionHistory_TransactionID" PRIMARY KEY ("TransactionID");


--
-- Name: UnitMeasure PK_UnitMeasure_UnitMeasureCode; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."UnitMeasure"
    ADD CONSTRAINT "PK_UnitMeasure_UnitMeasureCode" PRIMARY KEY ("UnitMeasureCode");


--
-- Name: WorkOrderRouting PK_WorkOrderRouting_WorkOrderID_ProductID_OperationSequence; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrderRouting"
    ADD CONSTRAINT "PK_WorkOrderRouting_WorkOrderID_ProductID_OperationSequence" PRIMARY KEY ("WorkOrderID", "ProductID", "OperationSequence");


--
-- Name: WorkOrder PK_WorkOrder_WorkOrderID; Type: CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrder"
    ADD CONSTRAINT "PK_WorkOrder_WorkOrderID" PRIMARY KEY ("WorkOrderID");


--
-- Name: ProductVendor PK_ProductVendor_ProductID_BusinessEntityID; Type: CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ProductVendor"
    ADD CONSTRAINT "PK_ProductVendor_ProductID_BusinessEntityID" PRIMARY KEY ("ProductID", "BusinessEntityID");


--
-- Name: PurchaseOrderDetail PK_PurchaseOrderDetail_PurchaseOrderID_PurchaseOrderDetailID; Type: CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderDetail"
    ADD CONSTRAINT "PK_PurchaseOrderDetail_PurchaseOrderID_PurchaseOrderDetailID" PRIMARY KEY ("PurchaseOrderID", "PurchaseOrderDetailID");


--
-- Name: PurchaseOrderHeader PK_PurchaseOrderHeader_PurchaseOrderID; Type: CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderHeader"
    ADD CONSTRAINT "PK_PurchaseOrderHeader_PurchaseOrderID" PRIMARY KEY ("PurchaseOrderID");


--
-- Name: ShipMethod PK_ShipMethod_ShipMethodID; Type: CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ShipMethod"
    ADD CONSTRAINT "PK_ShipMethod_ShipMethodID" PRIMARY KEY ("ShipMethodID");


--
-- Name: Vendor PK_Vendor_BusinessEntityID; Type: CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."Vendor"
    ADD CONSTRAINT "PK_Vendor_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: CountryRegionCurrency PK_CountryRegionCurrency_CountryRegionCode_CurrencyCode; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CountryRegionCurrency"
    ADD CONSTRAINT "PK_CountryRegionCurrency_CountryRegionCode_CurrencyCode" PRIMARY KEY ("CountryRegionCode", "CurrencyCode");


--
-- Name: CreditCard PK_CreditCard_CreditCardID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CreditCard"
    ADD CONSTRAINT "PK_CreditCard_CreditCardID" PRIMARY KEY ("CreditCardID");


--
-- Name: CurrencyRate PK_CurrencyRate_CurrencyRateID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CurrencyRate"
    ADD CONSTRAINT "PK_CurrencyRate_CurrencyRateID" PRIMARY KEY ("CurrencyRateID");


--
-- Name: Currency PK_Currency_CurrencyCode; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Currency"
    ADD CONSTRAINT "PK_Currency_CurrencyCode" PRIMARY KEY ("CurrencyCode");


--
-- Name: Customer PK_Customer_CustomerID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Customer"
    ADD CONSTRAINT "PK_Customer_CustomerID" PRIMARY KEY ("CustomerID");


--
-- Name: PersonCreditCard PK_PersonCreditCard_BusinessEntityID_CreditCardID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."PersonCreditCard"
    ADD CONSTRAINT "PK_PersonCreditCard_BusinessEntityID_CreditCardID" PRIMARY KEY ("BusinessEntityID", "CreditCardID");


--
-- Name: SalesOrderDetail PK_SalesOrderDetail_SalesOrderID_SalesOrderDetailID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderDetail"
    ADD CONSTRAINT "PK_SalesOrderDetail_SalesOrderID_SalesOrderDetailID" PRIMARY KEY ("SalesOrderID", "SalesOrderDetailID");


--
-- Name: SalesOrderHeaderSalesReason PK_SalesOrderHeaderSalesReason_SalesOrderID_SalesReasonID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeaderSalesReason"
    ADD CONSTRAINT "PK_SalesOrderHeaderSalesReason_SalesOrderID_SalesReasonID" PRIMARY KEY ("SalesOrderID", "SalesReasonID");


--
-- Name: SalesOrderHeader PK_SalesOrderHeader_SalesOrderID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "PK_SalesOrderHeader_SalesOrderID" PRIMARY KEY ("SalesOrderID");


--
-- Name: SalesPersonQuotaHistory PK_SalesPersonQuotaHistory_BusinessEntityID_QuotaDate; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesPersonQuotaHistory"
    ADD CONSTRAINT "PK_SalesPersonQuotaHistory_BusinessEntityID_QuotaDate" PRIMARY KEY ("BusinessEntityID", "QuotaDate");


--
-- Name: SalesPerson PK_SalesPerson_BusinessEntityID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesPerson"
    ADD CONSTRAINT "PK_SalesPerson_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: SalesReason PK_SalesReason_SalesReasonID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesReason"
    ADD CONSTRAINT "PK_SalesReason_SalesReasonID" PRIMARY KEY ("SalesReasonID");


--
-- Name: SalesTaxRate PK_SalesTaxRate_SalesTaxRateID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTaxRate"
    ADD CONSTRAINT "PK_SalesTaxRate_SalesTaxRateID" PRIMARY KEY ("SalesTaxRateID");


--
-- Name: SalesTerritoryHistory PK_SalesTerritoryHistory_BusinessEntityID_StartDate_TerritoryID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritoryHistory"
    ADD CONSTRAINT "PK_SalesTerritoryHistory_BusinessEntityID_StartDate_TerritoryID" PRIMARY KEY ("BusinessEntityID", "StartDate", "TerritoryID");


--
-- Name: SalesTerritory PK_SalesTerritory_TerritoryID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritory"
    ADD CONSTRAINT "PK_SalesTerritory_TerritoryID" PRIMARY KEY ("TerritoryID");


--
-- Name: ShoppingCartItem PK_ShoppingCartItem_ShoppingCartItemID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."ShoppingCartItem"
    ADD CONSTRAINT "PK_ShoppingCartItem_ShoppingCartItemID" PRIMARY KEY ("ShoppingCartItemID");


--
-- Name: SpecialOfferProduct PK_SpecialOfferProduct_SpecialOfferID_ProductID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SpecialOfferProduct"
    ADD CONSTRAINT "PK_SpecialOfferProduct_SpecialOfferID_ProductID" PRIMARY KEY ("SpecialOfferID", "ProductID");


--
-- Name: SpecialOffer PK_SpecialOffer_SpecialOfferID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SpecialOffer"
    ADD CONSTRAINT "PK_SpecialOffer_SpecialOfferID" PRIMARY KEY ("SpecialOfferID");


--
-- Name: Store PK_Store_BusinessEntityID; Type: CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Store"
    ADD CONSTRAINT "PK_Store_BusinessEntityID" PRIMARY KEY ("BusinessEntityID");


--
-- Name: AWBuildVersion PK_AWBuildVersion_SystemInformationID; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."AWBuildVersion"
    ADD CONSTRAINT "PK_AWBuildVersion_SystemInformationID" PRIMARY KEY ("SystemInformationID");


--
-- Name: DatabaseLog PK_DatabaseLog_DatabaseLogID; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."DatabaseLog"
    ADD CONSTRAINT "PK_DatabaseLog_DatabaseLogID" PRIMARY KEY ("DatabaseLogID");


--
-- Name: ErrorLog PK_ErrorLog_ErrorLogID; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."ErrorLog"
    ADD CONSTRAINT "PK_ErrorLog_ErrorLogID" PRIMARY KEY ("ErrorLogID");


--
-- Name: AK_Department_Name; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Department_Name" ON "HumanResources"."Department" USING btree ("Name");


--
-- Name: AK_Employee_LoginID; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Employee_LoginID" ON "HumanResources"."Employee" USING btree ("LoginID");


--
-- Name: AK_Employee_NationalIDNumber; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Employee_NationalIDNumber" ON "HumanResources"."Employee" USING btree ("NationalIDNumber");


--
-- Name: AK_Employee_rowguid; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Employee_rowguid" ON "HumanResources"."Employee" USING btree (rowguid);


--
-- Name: AK_Shift_Name; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Shift_Name" ON "HumanResources"."Shift" USING btree ("Name");


--
-- Name: AK_Shift_StartTime_EndTime; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE UNIQUE INDEX "AK_Shift_StartTime_EndTime" ON "HumanResources"."Shift" USING btree ("StartTime", "EndTime");


--
-- Name: IX_EmployeeDepartmentHistory_DepartmentID; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE INDEX "IX_EmployeeDepartmentHistory_DepartmentID" ON "HumanResources"."EmployeeDepartmentHistory" USING btree ("DepartmentID");


--
-- Name: IX_EmployeeDepartmentHistory_ShiftID; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE INDEX "IX_EmployeeDepartmentHistory_ShiftID" ON "HumanResources"."EmployeeDepartmentHistory" USING btree ("ShiftID");


--
-- Name: IX_Employee_OrganizationLevel_OrganizationNode; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE INDEX "IX_Employee_OrganizationLevel_OrganizationNode" ON "HumanResources"."Employee" USING btree ("OrganizationLevel", "OrganizationNode");


--
-- Name: IX_Employee_OrganizationNode; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE INDEX "IX_Employee_OrganizationNode" ON "HumanResources"."Employee" USING btree ("OrganizationNode");


--
-- Name: IX_JobCandidate_BusinessEntityID; Type: INDEX; Schema: HumanResources; Owner: -
--

CREATE INDEX "IX_JobCandidate_BusinessEntityID" ON "HumanResources"."JobCandidate" USING btree ("BusinessEntityID");


--
-- Name: AK_AddressType_Name; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_AddressType_Name" ON "Person"."AddressType" USING btree ("Name");


--
-- Name: AK_AddressType_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_AddressType_rowguid" ON "Person"."AddressType" USING btree (rowguid);


--
-- Name: AK_Address_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_Address_rowguid" ON "Person"."Address" USING btree (rowguid);


--
-- Name: AK_BusinessEntityAddress_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_BusinessEntityAddress_rowguid" ON "Person"."BusinessEntityAddress" USING btree (rowguid);


--
-- Name: AK_BusinessEntityContact_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_BusinessEntityContact_rowguid" ON "Person"."BusinessEntityContact" USING btree (rowguid);


--
-- Name: AK_BusinessEntity_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_BusinessEntity_rowguid" ON "Person"."BusinessEntity" USING btree (rowguid);


--
-- Name: AK_ContactType_Name; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_ContactType_Name" ON "Person"."ContactType" USING btree ("Name");


--
-- Name: AK_CountryRegion_Name; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_CountryRegion_Name" ON "Person"."CountryRegion" USING btree ("Name");


--
-- Name: AK_Person_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_Person_rowguid" ON "Person"."Person" USING btree (rowguid);


--
-- Name: AK_StateProvince_Name; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_StateProvince_Name" ON "Person"."StateProvince" USING btree ("Name");


--
-- Name: AK_StateProvince_StateProvinceCode_CountryRegionCode; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_StateProvince_StateProvinceCode_CountryRegionCode" ON "Person"."StateProvince" USING btree ("StateProvinceCode", "CountryRegionCode");


--
-- Name: AK_StateProvince_rowguid; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "AK_StateProvince_rowguid" ON "Person"."StateProvince" USING btree (rowguid);


--
-- Name: IX_Address_AddressLine1_AddressLine2_City_StateProvinceID_Posta; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "IX_Address_AddressLine1_AddressLine2_City_StateProvinceID_Posta" ON "Person"."Address" USING btree ("AddressLine1", "AddressLine2", "City", "StateProvinceID", "PostalCode");


--
-- Name: IX_Address_StateProvinceID; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_Address_StateProvinceID" ON "Person"."Address" USING btree ("StateProvinceID");


--
-- Name: IX_BusinessEntityAddress_AddressID; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_BusinessEntityAddress_AddressID" ON "Person"."BusinessEntityAddress" USING btree ("AddressID");


--
-- Name: IX_BusinessEntityAddress_AddressTypeID; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_BusinessEntityAddress_AddressTypeID" ON "Person"."BusinessEntityAddress" USING btree ("AddressTypeID");


--
-- Name: IX_BusinessEntityContact_ContactTypeID; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_BusinessEntityContact_ContactTypeID" ON "Person"."BusinessEntityContact" USING btree ("ContactTypeID");


--
-- Name: IX_BusinessEntityContact_PersonID; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_BusinessEntityContact_PersonID" ON "Person"."BusinessEntityContact" USING btree ("PersonID");


--
-- Name: IX_EmailAddress_EmailAddress; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_EmailAddress_EmailAddress" ON "Person"."EmailAddress" USING btree ("EmailAddress");


--
-- Name: IX_PersonPhone_PhoneNumber; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_PersonPhone_PhoneNumber" ON "Person"."PersonPhone" USING btree ("PhoneNumber");


--
-- Name: IX_Person_LastName_FirstName_MiddleName; Type: INDEX; Schema: Person; Owner: -
--

CREATE INDEX "IX_Person_LastName_FirstName_MiddleName" ON "Person"."Person" USING btree ("LastName", "FirstName", "MiddleName");


--
-- Name: IX_vStateProvinceCountryRegion; Type: INDEX; Schema: Person; Owner: -
--

CREATE UNIQUE INDEX "IX_vStateProvinceCountryRegion" ON "Person"."vStateProvinceCountryRegion" USING btree ("StateProvinceID", "CountryRegionCode");


--
-- Name: AK_BillOfMaterials_ProductAssemblyID_ComponentID_StartDate; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_BillOfMaterials_ProductAssemblyID_ComponentID_StartDate" ON "Production"."BillOfMaterials" USING btree ("ProductAssemblyID", "ComponentID", "StartDate");


--
-- Name: AK_Culture_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Culture_Name" ON "Production"."Culture" USING btree ("Name");


--
-- Name: AK_Document_DocumentLevel_DocumentNode; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Document_DocumentLevel_DocumentNode" ON "Production"."Document" USING btree ("DocumentLevel", "DocumentNode");


--
-- Name: AK_Document_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Document_rowguid" ON "Production"."Document" USING btree (rowguid);


--
-- Name: AK_Location_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Location_Name" ON "Production"."Location" USING btree ("Name");


--
-- Name: AK_ProductCategory_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductCategory_Name" ON "Production"."ProductCategory" USING btree ("Name");


--
-- Name: AK_ProductCategory_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductCategory_rowguid" ON "Production"."ProductCategory" USING btree (rowguid);


--
-- Name: AK_ProductDescription_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductDescription_rowguid" ON "Production"."ProductDescription" USING btree (rowguid);


--
-- Name: AK_ProductModel_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductModel_Name" ON "Production"."ProductModel" USING btree ("Name");


--
-- Name: AK_ProductModel_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductModel_rowguid" ON "Production"."ProductModel" USING btree (rowguid);


--
-- Name: AK_ProductSubcategory_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductSubcategory_Name" ON "Production"."ProductSubcategory" USING btree ("Name");


--
-- Name: AK_ProductSubcategory_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ProductSubcategory_rowguid" ON "Production"."ProductSubcategory" USING btree (rowguid);


--
-- Name: AK_Product_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Product_Name" ON "Production"."Product" USING btree ("Name");


--
-- Name: AK_Product_ProductNumber; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Product_ProductNumber" ON "Production"."Product" USING btree ("ProductNumber");


--
-- Name: AK_Product_rowguid; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_Product_rowguid" ON "Production"."Product" USING btree (rowguid);


--
-- Name: AK_ScrapReason_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_ScrapReason_Name" ON "Production"."ScrapReason" USING btree ("Name");


--
-- Name: AK_UnitMeasure_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "AK_UnitMeasure_Name" ON "Production"."UnitMeasure" USING btree ("Name");


--
-- Name: IX_BillOfMaterials_UnitMeasureCode; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_BillOfMaterials_UnitMeasureCode" ON "Production"."BillOfMaterials" USING btree ("UnitMeasureCode");


--
-- Name: IX_Document_FileName_Revision; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_Document_FileName_Revision" ON "Production"."Document" USING btree ("FileName", "Revision");


--
-- Name: IX_ProductReview_ProductID_Name; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_ProductReview_ProductID_Name" ON "Production"."ProductReview" USING btree ("ProductID", "ReviewerName") INCLUDE ("Comments");


--
-- Name: IX_TransactionHistoryArchive_ProductID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_TransactionHistoryArchive_ProductID" ON "Production"."TransactionHistoryArchive" USING btree ("ProductID");


--
-- Name: IX_TransactionHistoryArchive_ReferenceOrderID_ReferenceOrderLin; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_TransactionHistoryArchive_ReferenceOrderID_ReferenceOrderLin" ON "Production"."TransactionHistoryArchive" USING btree ("ReferenceOrderID", "ReferenceOrderLineID");


--
-- Name: IX_TransactionHistory_ProductID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_TransactionHistory_ProductID" ON "Production"."TransactionHistory" USING btree ("ProductID");


--
-- Name: IX_TransactionHistory_ReferenceOrderID_ReferenceOrderLineID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_TransactionHistory_ReferenceOrderID_ReferenceOrderLineID" ON "Production"."TransactionHistory" USING btree ("ReferenceOrderID", "ReferenceOrderLineID");


--
-- Name: IX_WorkOrderRouting_ProductID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_WorkOrderRouting_ProductID" ON "Production"."WorkOrderRouting" USING btree ("ProductID");


--
-- Name: IX_WorkOrder_ProductID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_WorkOrder_ProductID" ON "Production"."WorkOrder" USING btree ("ProductID");


--
-- Name: IX_WorkOrder_ScrapReasonID; Type: INDEX; Schema: Production; Owner: -
--

CREATE INDEX "IX_WorkOrder_ScrapReasonID" ON "Production"."WorkOrder" USING btree ("ScrapReasonID");


--
-- Name: IX_vProductAndDescription; Type: INDEX; Schema: Production; Owner: -
--

CREATE UNIQUE INDEX "IX_vProductAndDescription" ON "Production"."vProductAndDescription" USING btree ("CultureID", "ProductID");


--
-- Name: AK_ShipMethod_Name; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE UNIQUE INDEX "AK_ShipMethod_Name" ON "Purchasing"."ShipMethod" USING btree ("Name");


--
-- Name: AK_ShipMethod_rowguid; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE UNIQUE INDEX "AK_ShipMethod_rowguid" ON "Purchasing"."ShipMethod" USING btree (rowguid);


--
-- Name: AK_Vendor_AccountNumber; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE UNIQUE INDEX "AK_Vendor_AccountNumber" ON "Purchasing"."Vendor" USING btree ("AccountNumber");


--
-- Name: IX_ProductVendor_BusinessEntityID; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE INDEX "IX_ProductVendor_BusinessEntityID" ON "Purchasing"."ProductVendor" USING btree ("BusinessEntityID");


--
-- Name: IX_ProductVendor_UnitMeasureCode; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE INDEX "IX_ProductVendor_UnitMeasureCode" ON "Purchasing"."ProductVendor" USING btree ("UnitMeasureCode");


--
-- Name: IX_PurchaseOrderDetail_ProductID; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE INDEX "IX_PurchaseOrderDetail_ProductID" ON "Purchasing"."PurchaseOrderDetail" USING btree ("ProductID");


--
-- Name: IX_PurchaseOrderHeader_EmployeeID; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE INDEX "IX_PurchaseOrderHeader_EmployeeID" ON "Purchasing"."PurchaseOrderHeader" USING btree ("EmployeeID");


--
-- Name: IX_PurchaseOrderHeader_VendorID; Type: INDEX; Schema: Purchasing; Owner: -
--

CREATE INDEX "IX_PurchaseOrderHeader_VendorID" ON "Purchasing"."PurchaseOrderHeader" USING btree ("VendorID");


--
-- Name: AK_CreditCard_CardNumber; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_CreditCard_CardNumber" ON "Sales"."CreditCard" USING btree ("CardNumber");


--
-- Name: AK_CurrencyRate_CurrencyRateDate_FromCurrencyCode_ToCurrencyCod; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_CurrencyRate_CurrencyRateDate_FromCurrencyCode_ToCurrencyCod" ON "Sales"."CurrencyRate" USING btree ("CurrencyRateDate", "FromCurrencyCode", "ToCurrencyCode");


--
-- Name: AK_Currency_Name; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_Currency_Name" ON "Sales"."Currency" USING btree ("Name");


--
-- Name: AK_Customer_AccountNumber; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_Customer_AccountNumber" ON "Sales"."Customer" USING btree ("AccountNumber");


--
-- Name: AK_Customer_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_Customer_rowguid" ON "Sales"."Customer" USING btree (rowguid);


--
-- Name: AK_SalesOrderDetail_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesOrderDetail_rowguid" ON "Sales"."SalesOrderDetail" USING btree (rowguid);


--
-- Name: AK_SalesOrderHeader_SalesOrderNumber; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesOrderHeader_SalesOrderNumber" ON "Sales"."SalesOrderHeader" USING btree ("SalesOrderNumber");


--
-- Name: AK_SalesOrderHeader_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesOrderHeader_rowguid" ON "Sales"."SalesOrderHeader" USING btree (rowguid);


--
-- Name: AK_SalesPersonQuotaHistory_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesPersonQuotaHistory_rowguid" ON "Sales"."SalesPersonQuotaHistory" USING btree (rowguid);


--
-- Name: AK_SalesPerson_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesPerson_rowguid" ON "Sales"."SalesPerson" USING btree (rowguid);


--
-- Name: AK_SalesTaxRate_StateProvinceID_TaxType; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesTaxRate_StateProvinceID_TaxType" ON "Sales"."SalesTaxRate" USING btree ("StateProvinceID", "TaxType");


--
-- Name: AK_SalesTaxRate_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesTaxRate_rowguid" ON "Sales"."SalesTaxRate" USING btree (rowguid);


--
-- Name: AK_SalesTerritoryHistory_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesTerritoryHistory_rowguid" ON "Sales"."SalesTerritoryHistory" USING btree (rowguid);


--
-- Name: AK_SalesTerritory_Name; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesTerritory_Name" ON "Sales"."SalesTerritory" USING btree ("Name");


--
-- Name: AK_SalesTerritory_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SalesTerritory_rowguid" ON "Sales"."SalesTerritory" USING btree (rowguid);


--
-- Name: AK_SpecialOfferProduct_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SpecialOfferProduct_rowguid" ON "Sales"."SpecialOfferProduct" USING btree (rowguid);


--
-- Name: AK_SpecialOffer_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_SpecialOffer_rowguid" ON "Sales"."SpecialOffer" USING btree (rowguid);


--
-- Name: AK_Store_rowguid; Type: INDEX; Schema: Sales; Owner: -
--

CREATE UNIQUE INDEX "AK_Store_rowguid" ON "Sales"."Store" USING btree (rowguid);


--
-- Name: IX_CountryRegionCurrency_CurrencyCode; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_CountryRegionCurrency_CurrencyCode" ON "Sales"."CountryRegionCurrency" USING btree ("CurrencyCode");


--
-- Name: IX_Customer_TerritoryID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_Customer_TerritoryID" ON "Sales"."Customer" USING btree ("TerritoryID");


--
-- Name: IX_SalesOrderDetail_ProductID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_SalesOrderDetail_ProductID" ON "Sales"."SalesOrderDetail" USING btree ("ProductID");


--
-- Name: IX_SalesOrderHeader_CustomerID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_SalesOrderHeader_CustomerID" ON "Sales"."SalesOrderHeader" USING btree ("CustomerID");


--
-- Name: IX_SalesOrderHeader_SalesPersonID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_SalesOrderHeader_SalesPersonID" ON "Sales"."SalesOrderHeader" USING btree ("SalesPersonID");


--
-- Name: IX_ShoppingCartItem_ShoppingCartID_ProductID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_ShoppingCartItem_ShoppingCartID_ProductID" ON "Sales"."ShoppingCartItem" USING btree ("ShoppingCartID", "ProductID");


--
-- Name: IX_SpecialOfferProduct_ProductID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_SpecialOfferProduct_ProductID" ON "Sales"."SpecialOfferProduct" USING btree ("ProductID");


--
-- Name: IX_Store_SalesPersonID; Type: INDEX; Schema: Sales; Owner: -
--

CREATE INDEX "IX_Store_SalesPersonID" ON "Sales"."Store" USING btree ("SalesPersonID");


--
-- Name: EmployeeDepartmentHistory FK_EmployeeDepartmentHistory_Department_DepartmentID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeeDepartmentHistory"
    ADD CONSTRAINT "FK_EmployeeDepartmentHistory_Department_DepartmentID" FOREIGN KEY ("DepartmentID") REFERENCES "HumanResources"."Department"("DepartmentID");


--
-- Name: EmployeeDepartmentHistory FK_EmployeeDepartmentHistory_Employee_BusinessEntityID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeeDepartmentHistory"
    ADD CONSTRAINT "FK_EmployeeDepartmentHistory_Employee_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: EmployeeDepartmentHistory FK_EmployeeDepartmentHistory_Shift_ShiftID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeeDepartmentHistory"
    ADD CONSTRAINT "FK_EmployeeDepartmentHistory_Shift_ShiftID" FOREIGN KEY ("ShiftID") REFERENCES "HumanResources"."Shift"("ShiftID");


--
-- Name: EmployeePayHistory FK_EmployeePayHistory_Employee_BusinessEntityID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."EmployeePayHistory"
    ADD CONSTRAINT "FK_EmployeePayHistory_Employee_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: Employee FK_Employee_Person_BusinessEntityID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."Employee"
    ADD CONSTRAINT "FK_Employee_Person_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: JobCandidate FK_JobCandidate_Employee_BusinessEntityID; Type: FK CONSTRAINT; Schema: HumanResources; Owner: -
--

ALTER TABLE ONLY "HumanResources"."JobCandidate"
    ADD CONSTRAINT "FK_JobCandidate_Employee_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: Address FK_Address_StateProvince_StateProvinceID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Address"
    ADD CONSTRAINT "FK_Address_StateProvince_StateProvinceID" FOREIGN KEY ("StateProvinceID") REFERENCES "Person"."StateProvince"("StateProvinceID");


--
-- Name: BusinessEntityAddress FK_BusinessEntityAddress_AddressType_AddressTypeID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityAddress"
    ADD CONSTRAINT "FK_BusinessEntityAddress_AddressType_AddressTypeID" FOREIGN KEY ("AddressTypeID") REFERENCES "Person"."AddressType"("AddressTypeID");


--
-- Name: BusinessEntityAddress FK_BusinessEntityAddress_Address_AddressID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityAddress"
    ADD CONSTRAINT "FK_BusinessEntityAddress_Address_AddressID" FOREIGN KEY ("AddressID") REFERENCES "Person"."Address"("AddressID");


--
-- Name: BusinessEntityAddress FK_BusinessEntityAddress_BusinessEntity_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityAddress"
    ADD CONSTRAINT "FK_BusinessEntityAddress_BusinessEntity_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."BusinessEntity"("BusinessEntityID");


--
-- Name: BusinessEntityContact FK_BusinessEntityContact_BusinessEntity_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityContact"
    ADD CONSTRAINT "FK_BusinessEntityContact_BusinessEntity_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."BusinessEntity"("BusinessEntityID");


--
-- Name: BusinessEntityContact FK_BusinessEntityContact_ContactType_ContactTypeID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityContact"
    ADD CONSTRAINT "FK_BusinessEntityContact_ContactType_ContactTypeID" FOREIGN KEY ("ContactTypeID") REFERENCES "Person"."ContactType"("ContactTypeID");


--
-- Name: BusinessEntityContact FK_BusinessEntityContact_Person_PersonID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."BusinessEntityContact"
    ADD CONSTRAINT "FK_BusinessEntityContact_Person_PersonID" FOREIGN KEY ("PersonID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: EmailAddress FK_EmailAddress_Person_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."EmailAddress"
    ADD CONSTRAINT "FK_EmailAddress_Person_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: Password FK_Password_Person_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Password"
    ADD CONSTRAINT "FK_Password_Person_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: PersonPhone FK_PersonPhone_Person_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."PersonPhone"
    ADD CONSTRAINT "FK_PersonPhone_Person_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: PersonPhone FK_PersonPhone_PhoneNumberType_PhoneNumberTypeID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."PersonPhone"
    ADD CONSTRAINT "FK_PersonPhone_PhoneNumberType_PhoneNumberTypeID" FOREIGN KEY ("PhoneNumberTypeID") REFERENCES "Person"."PhoneNumberType"("PhoneNumberTypeID");


--
-- Name: Person FK_Person_BusinessEntity_BusinessEntityID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."Person"
    ADD CONSTRAINT "FK_Person_BusinessEntity_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."BusinessEntity"("BusinessEntityID");


--
-- Name: StateProvince FK_StateProvince_CountryRegion_CountryRegionCode; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."StateProvince"
    ADD CONSTRAINT "FK_StateProvince_CountryRegion_CountryRegionCode" FOREIGN KEY ("CountryRegionCode") REFERENCES "Person"."CountryRegion"("CountryRegionCode");


--
-- Name: StateProvince FK_StateProvince_SalesTerritory_TerritoryID; Type: FK CONSTRAINT; Schema: Person; Owner: -
--

ALTER TABLE ONLY "Person"."StateProvince"
    ADD CONSTRAINT "FK_StateProvince_SalesTerritory_TerritoryID" FOREIGN KEY ("TerritoryID") REFERENCES "Sales"."SalesTerritory"("TerritoryID");


--
-- Name: BillOfMaterials FK_BillOfMaterials_Product_ComponentID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."BillOfMaterials"
    ADD CONSTRAINT "FK_BillOfMaterials_Product_ComponentID" FOREIGN KEY ("ComponentID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: BillOfMaterials FK_BillOfMaterials_Product_ProductAssemblyID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."BillOfMaterials"
    ADD CONSTRAINT "FK_BillOfMaterials_Product_ProductAssemblyID" FOREIGN KEY ("ProductAssemblyID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: BillOfMaterials FK_BillOfMaterials_UnitMeasure_UnitMeasureCode; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."BillOfMaterials"
    ADD CONSTRAINT "FK_BillOfMaterials_UnitMeasure_UnitMeasureCode" FOREIGN KEY ("UnitMeasureCode") REFERENCES "Production"."UnitMeasure"("UnitMeasureCode");


--
-- Name: Document FK_Document_Employee_Owner; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Document"
    ADD CONSTRAINT "FK_Document_Employee_Owner" FOREIGN KEY ("Owner") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: ProductCostHistory FK_ProductCostHistory_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductCostHistory"
    ADD CONSTRAINT "FK_ProductCostHistory_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductDocument FK_ProductDocument_Document_DocumentNode; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductDocument"
    ADD CONSTRAINT "FK_ProductDocument_Document_DocumentNode" FOREIGN KEY ("DocumentNode") REFERENCES "Production"."Document"("DocumentNode");


--
-- Name: ProductDocument FK_ProductDocument_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductDocument"
    ADD CONSTRAINT "FK_ProductDocument_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductInventory FK_ProductInventory_Location_LocationID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductInventory"
    ADD CONSTRAINT "FK_ProductInventory_Location_LocationID" FOREIGN KEY ("LocationID") REFERENCES "Production"."Location"("LocationID");


--
-- Name: ProductInventory FK_ProductInventory_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductInventory"
    ADD CONSTRAINT "FK_ProductInventory_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductListPriceHistory FK_ProductListPriceHistory_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductListPriceHistory"
    ADD CONSTRAINT "FK_ProductListPriceHistory_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductModelIllustration FK_ProductModelIllustration_Illustration_IllustrationID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelIllustration"
    ADD CONSTRAINT "FK_ProductModelIllustration_Illustration_IllustrationID" FOREIGN KEY ("IllustrationID") REFERENCES "Production"."Illustration"("IllustrationID");


--
-- Name: ProductModelIllustration FK_ProductModelIllustration_ProductModel_ProductModelID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelIllustration"
    ADD CONSTRAINT "FK_ProductModelIllustration_ProductModel_ProductModelID" FOREIGN KEY ("ProductModelID") REFERENCES "Production"."ProductModel"("ProductModelID");


--
-- Name: ProductModelProductDescriptionCulture FK_ProductModelProductDescriptionCulture_Culture_CultureID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelProductDescriptionCulture"
    ADD CONSTRAINT "FK_ProductModelProductDescriptionCulture_Culture_CultureID" FOREIGN KEY ("CultureID") REFERENCES "Production"."Culture"("CultureID");


--
-- Name: ProductModelProductDescriptionCulture FK_ProductModelProductDescriptionCulture_ProductDescription_Pro; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelProductDescriptionCulture"
    ADD CONSTRAINT "FK_ProductModelProductDescriptionCulture_ProductDescription_Pro" FOREIGN KEY ("ProductDescriptionID") REFERENCES "Production"."ProductDescription"("ProductDescriptionID");


--
-- Name: ProductModelProductDescriptionCulture FK_ProductModelProductDescriptionCulture_ProductModel_ProductMo; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductModelProductDescriptionCulture"
    ADD CONSTRAINT "FK_ProductModelProductDescriptionCulture_ProductModel_ProductMo" FOREIGN KEY ("ProductModelID") REFERENCES "Production"."ProductModel"("ProductModelID");


--
-- Name: ProductProductPhoto FK_ProductProductPhoto_ProductPhoto_ProductPhotoID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductProductPhoto"
    ADD CONSTRAINT "FK_ProductProductPhoto_ProductPhoto_ProductPhotoID" FOREIGN KEY ("ProductPhotoID") REFERENCES "Production"."ProductPhoto"("ProductPhotoID");


--
-- Name: ProductProductPhoto FK_ProductProductPhoto_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductProductPhoto"
    ADD CONSTRAINT "FK_ProductProductPhoto_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductReview FK_ProductReview_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductReview"
    ADD CONSTRAINT "FK_ProductReview_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductSubcategory FK_ProductSubcategory_ProductCategory_ProductCategoryID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."ProductSubcategory"
    ADD CONSTRAINT "FK_ProductSubcategory_ProductCategory_ProductCategoryID" FOREIGN KEY ("ProductCategoryID") REFERENCES "Production"."ProductCategory"("ProductCategoryID");


--
-- Name: Product FK_Product_ProductModel_ProductModelID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product"
    ADD CONSTRAINT "FK_Product_ProductModel_ProductModelID" FOREIGN KEY ("ProductModelID") REFERENCES "Production"."ProductModel"("ProductModelID");


--
-- Name: Product FK_Product_ProductSubcategory_ProductSubcategoryID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product"
    ADD CONSTRAINT "FK_Product_ProductSubcategory_ProductSubcategoryID" FOREIGN KEY ("ProductSubcategoryID") REFERENCES "Production"."ProductSubcategory"("ProductSubcategoryID");


--
-- Name: Product FK_Product_UnitMeasure_SizeUnitMeasureCode; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product"
    ADD CONSTRAINT "FK_Product_UnitMeasure_SizeUnitMeasureCode" FOREIGN KEY ("SizeUnitMeasureCode") REFERENCES "Production"."UnitMeasure"("UnitMeasureCode");


--
-- Name: Product FK_Product_UnitMeasure_WeightUnitMeasureCode; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."Product"
    ADD CONSTRAINT "FK_Product_UnitMeasure_WeightUnitMeasureCode" FOREIGN KEY ("WeightUnitMeasureCode") REFERENCES "Production"."UnitMeasure"("UnitMeasureCode");


--
-- Name: TransactionHistory FK_TransactionHistory_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."TransactionHistory"
    ADD CONSTRAINT "FK_TransactionHistory_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: WorkOrderRouting FK_WorkOrderRouting_Location_LocationID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrderRouting"
    ADD CONSTRAINT "FK_WorkOrderRouting_Location_LocationID" FOREIGN KEY ("LocationID") REFERENCES "Production"."Location"("LocationID");


--
-- Name: WorkOrderRouting FK_WorkOrderRouting_WorkOrder_WorkOrderID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrderRouting"
    ADD CONSTRAINT "FK_WorkOrderRouting_WorkOrder_WorkOrderID" FOREIGN KEY ("WorkOrderID") REFERENCES "Production"."WorkOrder"("WorkOrderID");


--
-- Name: WorkOrder FK_WorkOrder_Product_ProductID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrder"
    ADD CONSTRAINT "FK_WorkOrder_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: WorkOrder FK_WorkOrder_ScrapReason_ScrapReasonID; Type: FK CONSTRAINT; Schema: Production; Owner: -
--

ALTER TABLE ONLY "Production"."WorkOrder"
    ADD CONSTRAINT "FK_WorkOrder_ScrapReason_ScrapReasonID" FOREIGN KEY ("ScrapReasonID") REFERENCES "Production"."ScrapReason"("ScrapReasonID");


--
-- Name: ProductVendor FK_ProductVendor_Product_ProductID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ProductVendor"
    ADD CONSTRAINT "FK_ProductVendor_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: ProductVendor FK_ProductVendor_UnitMeasure_UnitMeasureCode; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ProductVendor"
    ADD CONSTRAINT "FK_ProductVendor_UnitMeasure_UnitMeasureCode" FOREIGN KEY ("UnitMeasureCode") REFERENCES "Production"."UnitMeasure"("UnitMeasureCode");


--
-- Name: ProductVendor FK_ProductVendor_Vendor_BusinessEntityID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."ProductVendor"
    ADD CONSTRAINT "FK_ProductVendor_Vendor_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Purchasing"."Vendor"("BusinessEntityID");


--
-- Name: PurchaseOrderDetail FK_PurchaseOrderDetail_Product_ProductID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderDetail"
    ADD CONSTRAINT "FK_PurchaseOrderDetail_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: PurchaseOrderDetail FK_PurchaseOrderDetail_PurchaseOrderHeader_PurchaseOrderID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderDetail"
    ADD CONSTRAINT "FK_PurchaseOrderDetail_PurchaseOrderHeader_PurchaseOrderID" FOREIGN KEY ("PurchaseOrderID") REFERENCES "Purchasing"."PurchaseOrderHeader"("PurchaseOrderID");


--
-- Name: PurchaseOrderHeader FK_PurchaseOrderHeader_Employee_EmployeeID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderHeader"
    ADD CONSTRAINT "FK_PurchaseOrderHeader_Employee_EmployeeID" FOREIGN KEY ("EmployeeID") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: PurchaseOrderHeader FK_PurchaseOrderHeader_ShipMethod_ShipMethodID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderHeader"
    ADD CONSTRAINT "FK_PurchaseOrderHeader_ShipMethod_ShipMethodID" FOREIGN KEY ("ShipMethodID") REFERENCES "Purchasing"."ShipMethod"("ShipMethodID");


--
-- Name: PurchaseOrderHeader FK_PurchaseOrderHeader_Vendor_VendorID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."PurchaseOrderHeader"
    ADD CONSTRAINT "FK_PurchaseOrderHeader_Vendor_VendorID" FOREIGN KEY ("VendorID") REFERENCES "Purchasing"."Vendor"("BusinessEntityID");


--
-- Name: Vendor FK_Vendor_BusinessEntity_BusinessEntityID; Type: FK CONSTRAINT; Schema: Purchasing; Owner: -
--

ALTER TABLE ONLY "Purchasing"."Vendor"
    ADD CONSTRAINT "FK_Vendor_BusinessEntity_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."BusinessEntity"("BusinessEntityID");


--
-- Name: CountryRegionCurrency FK_CountryRegionCurrency_CountryRegion_CountryRegionCode; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CountryRegionCurrency"
    ADD CONSTRAINT "FK_CountryRegionCurrency_CountryRegion_CountryRegionCode" FOREIGN KEY ("CountryRegionCode") REFERENCES "Person"."CountryRegion"("CountryRegionCode");


--
-- Name: CountryRegionCurrency FK_CountryRegionCurrency_Currency_CurrencyCode; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CountryRegionCurrency"
    ADD CONSTRAINT "FK_CountryRegionCurrency_Currency_CurrencyCode" FOREIGN KEY ("CurrencyCode") REFERENCES "Sales"."Currency"("CurrencyCode");


--
-- Name: CurrencyRate FK_CurrencyRate_Currency_FromCurrencyCode; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CurrencyRate"
    ADD CONSTRAINT "FK_CurrencyRate_Currency_FromCurrencyCode" FOREIGN KEY ("FromCurrencyCode") REFERENCES "Sales"."Currency"("CurrencyCode");


--
-- Name: CurrencyRate FK_CurrencyRate_Currency_ToCurrencyCode; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."CurrencyRate"
    ADD CONSTRAINT "FK_CurrencyRate_Currency_ToCurrencyCode" FOREIGN KEY ("ToCurrencyCode") REFERENCES "Sales"."Currency"("CurrencyCode");


--
-- Name: Customer FK_Customer_Person_PersonID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Customer"
    ADD CONSTRAINT "FK_Customer_Person_PersonID" FOREIGN KEY ("PersonID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: Customer FK_Customer_SalesTerritory_TerritoryID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Customer"
    ADD CONSTRAINT "FK_Customer_SalesTerritory_TerritoryID" FOREIGN KEY ("TerritoryID") REFERENCES "Sales"."SalesTerritory"("TerritoryID");


--
-- Name: Customer FK_Customer_Store_StoreID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Customer"
    ADD CONSTRAINT "FK_Customer_Store_StoreID" FOREIGN KEY ("StoreID") REFERENCES "Sales"."Store"("BusinessEntityID");


--
-- Name: PersonCreditCard FK_PersonCreditCard_CreditCard_CreditCardID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."PersonCreditCard"
    ADD CONSTRAINT "FK_PersonCreditCard_CreditCard_CreditCardID" FOREIGN KEY ("CreditCardID") REFERENCES "Sales"."CreditCard"("CreditCardID");


--
-- Name: PersonCreditCard FK_PersonCreditCard_Person_BusinessEntityID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."PersonCreditCard"
    ADD CONSTRAINT "FK_PersonCreditCard_Person_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."Person"("BusinessEntityID");


--
-- Name: SalesOrderDetail FK_SalesOrderDetail_SalesOrderHeader_SalesOrderID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderDetail"
    ADD CONSTRAINT "FK_SalesOrderDetail_SalesOrderHeader_SalesOrderID" FOREIGN KEY ("SalesOrderID") REFERENCES "Sales"."SalesOrderHeader"("SalesOrderID") ON DELETE CASCADE;


--
-- Name: SalesOrderDetail FK_SalesOrderDetail_SpecialOfferProduct_SpecialOfferIDProductID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderDetail"
    ADD CONSTRAINT "FK_SalesOrderDetail_SpecialOfferProduct_SpecialOfferIDProductID" FOREIGN KEY ("SpecialOfferID", "ProductID") REFERENCES "Sales"."SpecialOfferProduct"("SpecialOfferID", "ProductID");


--
-- Name: SalesOrderHeaderSalesReason FK_SalesOrderHeaderSalesReason_SalesOrderHeader_SalesOrderID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeaderSalesReason"
    ADD CONSTRAINT "FK_SalesOrderHeaderSalesReason_SalesOrderHeader_SalesOrderID" FOREIGN KEY ("SalesOrderID") REFERENCES "Sales"."SalesOrderHeader"("SalesOrderID") ON DELETE CASCADE;


--
-- Name: SalesOrderHeaderSalesReason FK_SalesOrderHeaderSalesReason_SalesReason_SalesReasonID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeaderSalesReason"
    ADD CONSTRAINT "FK_SalesOrderHeaderSalesReason_SalesReason_SalesReasonID" FOREIGN KEY ("SalesReasonID") REFERENCES "Sales"."SalesReason"("SalesReasonID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_Address_BillToAddressID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_Address_BillToAddressID" FOREIGN KEY ("BillToAddressID") REFERENCES "Person"."Address"("AddressID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_Address_ShipToAddressID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_Address_ShipToAddressID" FOREIGN KEY ("ShipToAddressID") REFERENCES "Person"."Address"("AddressID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_CreditCard_CreditCardID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_CreditCard_CreditCardID" FOREIGN KEY ("CreditCardID") REFERENCES "Sales"."CreditCard"("CreditCardID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_CurrencyRate_CurrencyRateID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_CurrencyRate_CurrencyRateID" FOREIGN KEY ("CurrencyRateID") REFERENCES "Sales"."CurrencyRate"("CurrencyRateID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_Customer_CustomerID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_Customer_CustomerID" FOREIGN KEY ("CustomerID") REFERENCES "Sales"."Customer"("CustomerID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_SalesPerson_SalesPersonID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_SalesPerson_SalesPersonID" FOREIGN KEY ("SalesPersonID") REFERENCES "Sales"."SalesPerson"("BusinessEntityID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_SalesTerritory_TerritoryID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_SalesTerritory_TerritoryID" FOREIGN KEY ("TerritoryID") REFERENCES "Sales"."SalesTerritory"("TerritoryID");


--
-- Name: SalesOrderHeader FK_SalesOrderHeader_ShipMethod_ShipMethodID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesOrderHeader"
    ADD CONSTRAINT "FK_SalesOrderHeader_ShipMethod_ShipMethodID" FOREIGN KEY ("ShipMethodID") REFERENCES "Purchasing"."ShipMethod"("ShipMethodID");


--
-- Name: SalesPersonQuotaHistory FK_SalesPersonQuotaHistory_SalesPerson_BusinessEntityID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesPersonQuotaHistory"
    ADD CONSTRAINT "FK_SalesPersonQuotaHistory_SalesPerson_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Sales"."SalesPerson"("BusinessEntityID");


--
-- Name: SalesPerson FK_SalesPerson_Employee_BusinessEntityID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesPerson"
    ADD CONSTRAINT "FK_SalesPerson_Employee_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "HumanResources"."Employee"("BusinessEntityID");


--
-- Name: SalesPerson FK_SalesPerson_SalesTerritory_TerritoryID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesPerson"
    ADD CONSTRAINT "FK_SalesPerson_SalesTerritory_TerritoryID" FOREIGN KEY ("TerritoryID") REFERENCES "Sales"."SalesTerritory"("TerritoryID");


--
-- Name: SalesTaxRate FK_SalesTaxRate_StateProvince_StateProvinceID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTaxRate"
    ADD CONSTRAINT "FK_SalesTaxRate_StateProvince_StateProvinceID" FOREIGN KEY ("StateProvinceID") REFERENCES "Person"."StateProvince"("StateProvinceID");


--
-- Name: SalesTerritoryHistory FK_SalesTerritoryHistory_SalesPerson_BusinessEntityID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritoryHistory"
    ADD CONSTRAINT "FK_SalesTerritoryHistory_SalesPerson_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Sales"."SalesPerson"("BusinessEntityID");


--
-- Name: SalesTerritoryHistory FK_SalesTerritoryHistory_SalesTerritory_TerritoryID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritoryHistory"
    ADD CONSTRAINT "FK_SalesTerritoryHistory_SalesTerritory_TerritoryID" FOREIGN KEY ("TerritoryID") REFERENCES "Sales"."SalesTerritory"("TerritoryID");


--
-- Name: SalesTerritory FK_SalesTerritory_CountryRegion_CountryRegionCode; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SalesTerritory"
    ADD CONSTRAINT "FK_SalesTerritory_CountryRegion_CountryRegionCode" FOREIGN KEY ("CountryRegionCode") REFERENCES "Person"."CountryRegion"("CountryRegionCode");


--
-- Name: ShoppingCartItem FK_ShoppingCartItem_Product_ProductID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."ShoppingCartItem"
    ADD CONSTRAINT "FK_ShoppingCartItem_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: SpecialOfferProduct FK_SpecialOfferProduct_Product_ProductID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SpecialOfferProduct"
    ADD CONSTRAINT "FK_SpecialOfferProduct_Product_ProductID" FOREIGN KEY ("ProductID") REFERENCES "Production"."Product"("ProductID");


--
-- Name: SpecialOfferProduct FK_SpecialOfferProduct_SpecialOffer_SpecialOfferID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."SpecialOfferProduct"
    ADD CONSTRAINT "FK_SpecialOfferProduct_SpecialOffer_SpecialOfferID" FOREIGN KEY ("SpecialOfferID") REFERENCES "Sales"."SpecialOffer"("SpecialOfferID");


--
-- Name: Store FK_Store_BusinessEntity_BusinessEntityID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Store"
    ADD CONSTRAINT "FK_Store_BusinessEntity_BusinessEntityID" FOREIGN KEY ("BusinessEntityID") REFERENCES "Person"."BusinessEntity"("BusinessEntityID");


--
-- Name: Store FK_Store_SalesPerson_SalesPersonID; Type: FK CONSTRAINT; Schema: Sales; Owner: -
--

ALTER TABLE ONLY "Sales"."Store"
    ADD CONSTRAINT "FK_Store_SalesPerson_SalesPersonID" FOREIGN KEY ("SalesPersonID") REFERENCES "Sales"."SalesPerson"("BusinessEntityID");


--
-- PostgreSQL database dump complete
--

