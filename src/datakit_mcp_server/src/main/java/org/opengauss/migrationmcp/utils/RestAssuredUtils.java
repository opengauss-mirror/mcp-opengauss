package org.opengauss.migrationmcp.utils;

import io.restassured.RestAssured;
import io.restassured.config.SSLConfig;

public class RestAssuredUtils {
    /**
     * 设置rest请求的服务地址
     *
     * @param baseUri 服务地址
     */
    public static void setBaseUri(String baseUri) {
        RestAssured.baseURI = baseUri;
        RestAssured.config = RestAssured.config().sslConfig(new SSLConfig().relaxedHTTPSValidation());
    }

    /**
     * 设置rest请求的根路径
     *
     * @param basePath 根路径
     */
    public static void setBasePath(String basePath) {
        RestAssured.basePath = basePath;
    }
}
