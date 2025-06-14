package org.opengauss.migrationmcp.service;

import io.restassured.RestAssured;
import io.restassured.http.ContentType;
import io.restassured.http.Header;
import io.restassured.response.Response;
import org.hamcrest.Matchers;
import org.opengauss.migrationmcp.config.ApplicationConfig;
import org.opengauss.migrationmcp.entity.SystemUser;
import org.opengauss.migrationmcp.utils.EncryptUtils;
import org.opengauss.migrationmcp.utils.RestAssuredUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.stereotype.Component;

import java.net.ConnectException;

@Component
public class LoginService {
    @Autowired
    private ApplicationConfig applicationConfig;

    @Bean("tokenHeader")
    public Header login() {
        RestAssuredUtils.setBaseUri(applicationConfig.getServerUrl());

        SystemUser systemUser = new SystemUser(
                applicationConfig.getUsername(), EncryptUtils.encrypt(applicationConfig.getPassword()));

        Response response;
        try {
            response = RestAssured.given()
                    .contentType(ContentType.JSON)
                    .body(systemUser)
                    .when()
                    .post("/login");
        } catch (Exception e) {
            if (e instanceof ConnectException) {
                throw new RuntimeException("无法连接到DataKit服务，请检查服务是否正在运行");
            } else {
                throw new RuntimeException(e);
            }
        }

        try {
            response.then()
                    .body("code", Matchers.equalTo(200));
        } catch (AssertionError e) {
            throw new RuntimeException("无法登录DataKit服务，请确保用户" + applicationConfig.getUsername() + "密码正确");
        }

        String token = response.jsonPath().getString("token");
        return new Header("Authorization", "Bearer " + token);
    }
}
